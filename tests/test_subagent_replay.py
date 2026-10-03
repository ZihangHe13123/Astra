"""Recovery must fit the provider request without losing active evidence."""

import asyncio
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.session_store import SessionStore
from agent.runtime.subagent_replay import (
    ReplayBudgetExceeded,
    bound_messages,
    replay_budget,
)
from agent.runtime.token_estimator import estimate_messages_tokens, estimate_value_tokens
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class OfflineLLM:
    def __init__(self, **config):
        self.config = SimpleNamespace(max_tokens=4096, **config)
        self.summary_calls = 0

    def estimate_tokens(self, messages):
        return estimate_messages_tokens(messages)

    async def chat_limited(self, *args, **kwargs):
        self.summary_calls += 1
        raise RuntimeError("No summary service available")


def registry(*, description="read", local=False):
    tools = ToolRegistry()
    tools.register(ToolDef(
        "read_file", description, {"type": "object", "properties": {}},
        lambda: None, risk="read",
    ))
    tools.register(ToolDef(
        "write_file", "write", {"type": "object", "properties": {}},
        lambda: None, risk="write",
    ))
    if local:
        tools.register(ToolDef(
            "snapshot", "snapshot", {"type": "object", "properties": {}},
            lambda: None, risk="read", result_persistence="request_local",
        ))
    return tools


def step(index, name="read_file", content=None):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{index}", "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": f"c{index}",
         "content": content if content is not None else f"evidence {index} " * 200},
    ]


def history():
    messages = [{"role": "system", "content": "You are a worker."},
                {"role": "user", "content": "Investigate the issue."}]
    for index in range(10):
        messages.extend(step(index))
    messages.append({"role": "user", "content": "Now fix only the parser."})
    return messages


def cost(messages, tools):
    return estimate_messages_tokens(messages) + estimate_value_tokens(tools.to_openai_tools())


def test_small_replay_is_unchanged_and_independent():
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "new instruction"}]
    result = asyncio.run(bound_messages(messages, registry=registry(), llm=OfflineLLM()))
    assert result == messages
    assert result is not messages
    result[-1]["content"] = "changed"
    assert messages[-1]["content"] == "new instruction"


@pytest.mark.parametrize("tool_name", ["write_file", "removed_plugin", "snapshot", "network_probe"])
def test_deterministic_compaction_retains_risky_unknown_and_request_local(tool_name):
    tools = registry(local=True)
    if tool_name == "network_probe":
        tools.register(ToolDef(
            "network_probe", "network", {"type": "object", "properties": {}},
            lambda: None, risk="network",
        ))
    messages = history()
    messages[2:2] = step("protected", tool_name, "Important durable effect or local evidence. " * 20)
    original = copy.deepcopy(messages)
    budget = cost(messages, tools) - 1500
    result = asyncio.run(bound_messages(
        messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=budget,
    ))
    assert cost(result, tools) <= budget
    assert step("protected", tool_name, "Important durable effect or local evidence. " * 20)[1] in result
    assert messages == original
    assert result[-1] == messages[-1]
    for index in range(6, 10):
        assert step(index)[0] in result
        assert step(index)[1] in result


@pytest.mark.parametrize("tool_name", ["team_send", "team_inbox", "team_task", "team"])
def test_compaction_preserves_coordination_effects_despite_read_approval_risk(tool_name):
    tools = registry()
    tools.register(ToolDef(
        tool_name, "coordination", {"type": "object", "properties": {}},
        lambda: None, risk="read",
    ))
    evidence = step("coordination", tool_name, "Task state changed; message delivered.")
    messages = history()
    messages[2:2] = evidence
    result = asyncio.run(bound_messages(
        messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=cost(messages, tools) - 1500,
    ))
    assert all(message in result for message in evidence)
    assert "Completed tool step: read_file" in str(result)


def test_schema_overhead_is_part_of_budget():
    tools = registry(description="large schema description " * 300)
    messages = [{"role": "user", "content": "small request"}]
    with pytest.raises(ReplayBudgetExceeded, match="budget"):
        asyncio.run(bound_messages(messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=200))


@pytest.mark.parametrize("no_tools", [False, True])
def test_schema_budget_charges_only_tools_exposed_by_this_request(no_tools):
    tools = registry(description="unused large schema description " * 1000)
    messages = [{"role": "user", "content": "Produce a small report"}]
    schemas = [] if no_tools else tools.to_openai_tools(names={"write_file"})
    result = asyncio.run(bound_messages(
        messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=200,
        tool_schemas=schemas,
    ))
    assert result == messages
    # The same history really is too large when the full schema is exposed.
    with pytest.raises(ReplayBudgetExceeded):
        asyncio.run(bound_messages(messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=200))


def test_fixed_system_overhead_is_rejected_without_summary():
    llm = OfflineLLM()
    messages = [{"role": "system", "content": "fixed system instructions " * 1000},
                {"role": "user", "content": "do the work"}]
    with pytest.raises(ReplayBudgetExceeded, match="budget"):
        asyncio.run(bound_messages(messages, registry=registry(), llm=llm, max_prompt_tokens=1000))
    assert llm.summary_calls == 0


def test_active_request_and_tool_chain_are_not_silently_truncated():
    llm = OfflineLLM()
    messages = [{"role": "user", "content": "Read this source and fix it"},
                *step(1, content="active source " * 2000)]
    original = copy.deepcopy(messages)
    with pytest.raises(ReplayBudgetExceeded, match="budget"):
        asyncio.run(bound_messages(messages, registry=registry(), llm=llm, max_prompt_tokens=1000))
    assert messages == original
    assert llm.summary_calls == 0


def test_unavailable_summary_fails_without_erasing_history():
    llm = OfflineLLM()
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "old task"},
                {"role": "assistant", "content": "historical context " * 5000},
                {"role": "user", "content": "new task"}]
    original = copy.deepcopy(messages)
    with pytest.raises(ReplayBudgetExceeded, match="budget"):
        asyncio.run(bound_messages(messages, registry=registry(), llm=llm, max_prompt_tokens=1000))
    assert messages == original


def test_real_summary_preserves_latest_instruction(monkeypatch):
    from agent.runtime.context_compressor import ContextCompressor

    calls = []

    async def summary(self, turns, **kwargs):
        calls.append(turns)
        return "[CONTEXT COMPACTION — REFERENCE ONLY] The old investigation is complete."

    monkeypatch.setattr(ContextCompressor, "_generate_summary", summary)
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "old task"},
                *[{"role": "assistant", "content": "historical context " * 20} for _ in range(30)],
                {"role": "user", "content": "new task, preserve this verbatim"}]
    result = asyncio.run(bound_messages(messages, registry=registry(), llm=OfflineLLM(), max_prompt_tokens=1500))
    assert calls
    assert messages[-1] in result
    assert result[0] == messages[0]
    assert cost(result, registry()) <= 1500


def test_compressor_cannot_rewrite_active_instruction(monkeypatch):
    from agent.runtime.context_compressor import ContextCompressor

    async def bad_compression(self, messages, *args, **kwargs):
        return [messages[0], {"role": "user", "content": "silently rewritten"}]

    monkeypatch.setattr(ContextCompressor, "compress", bad_compression)
    messages = [{"role": "system", "content": "system"},
                {"role": "assistant", "content": "old evidence " * 2000},
                {"role": "user", "content": "preserve this exact instruction"}]
    original = copy.deepcopy(messages)
    with pytest.raises(ReplayBudgetExceeded, match="active/recent"):
        asyncio.run(bound_messages(messages, registry=registry(), llm=OfflineLLM(), max_prompt_tokens=1000))
    assert messages == original


@pytest.mark.parametrize("tool_name", ["write_file", "removed_plugin", "snapshot"])
def test_llm_summary_cannot_erase_older_effect_or_request_local_evidence(tool_name):
    class SummarizingLLM(OfflineLLM):
        async def chat_limited(self, *args, **kwargs):
            self.summary_calls += 1
            return {"content": "Earlier investigation complete."}

    llm = SummarizingLLM()
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "old task"},
                *step("effect", tool_name, "COMMIT-UNIQUE-WRITE-EVIDENCE"),
                *[{"role": "assistant", "content": "historical context " * 20} for _ in range(30)]]
    for index in range(6):
        messages.extend(step(index, content=f"read evidence {index}"))
    messages.append({"role": "user", "content": "new task"})
    original = copy.deepcopy(messages)
    with pytest.raises(ReplayBudgetExceeded, match="historical tool evidence"):
        asyncio.run(bound_messages(
            messages, registry=registry(local=True), llm=llm, max_prompt_tokens=1500,
        ))
    assert llm.summary_calls > 0
    assert messages == original


@pytest.mark.parametrize("compact", [False, True])
def test_dispatch_write_risk_survives_registry_change_and_metadata_is_private(compact):
    tools = registry()
    tools.register(ToolDef(
        "changed_tool", "now read-only", {"type": "object", "properties": {}},
        lambda: None, risk="read",
    ))
    messages = history()
    messages[2]["tool_calls"][0]["function"]["name"] = "changed_tool"
    messages[2]["_recovery_tool_risks"] = {"c0": "write"}
    original = copy.deepcopy(messages)
    budget = cost(messages, tools) - 1500 if compact else 64000
    result = asyncio.run(bound_messages(
        messages, registry=tools, llm=OfflineLLM(), max_prompt_tokens=budget,
    ))
    assert step(0, "changed_tool")[0] in result
    assert step(0)[1] in result
    assert all("_recovery_tool_risks" not in message for message in result)
    assert messages == original


def test_mixed_tool_group_is_preserved_as_one_evidence_chain(monkeypatch):
    from agent.runtime.context_compressor import ContextCompressor

    evidence = step("write", "write_file", "WRITE-COMPANION")
    read = step("read", "read_file", "READ-COMPANION")
    evidence[0]["tool_calls"].extend(read[0]["tool_calls"])
    evidence.append(read[1])
    messages = history()
    messages[2:2] = evidence

    async def incomplete_summary(self, source, *args, **kwargs):
        # A summary that keeps the write but loses the read from its mixed
        # tool group must not create an incomplete provider protocol chain.
        return [message for message in source if message.get("content") != "READ-COMPANION"]

    monkeypatch.setattr(ContextCompressor, "compress", incomplete_summary)
    # Raise the cost of an old prose message so deterministic read cleanup
    # alone cannot meet this budget and the summary stage is exercised.
    messages.insert(2, {"role": "assistant", "content": "old prose " * 3000})
    with pytest.raises(ReplayBudgetExceeded, match="historical tool evidence"):
        asyncio.run(bound_messages(messages, registry=registry(), llm=OfflineLLM(), max_prompt_tokens=3000))


def test_budget_is_clipped_to_provider_capacity_and_compaction(monkeypatch):
    monkeypatch.setenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", "64000")
    llm = OfflineLLM(context_limit=16000, auto_compact_token_limit=14000)
    assert replay_budget(llm) == 11904
    llm.config.auto_compact_token_limit = 9000
    assert replay_budget(llm) == 9000
    monkeypatch.setenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", "7000")
    assert replay_budget(llm) == 7000


def test_explicit_budget_cannot_override_provider_capacity():
    llm = OfflineLLM(context_limit=4200)
    with pytest.raises(ReplayBudgetExceeded):
        asyncio.run(bound_messages(history(), registry=registry(), llm=llm, max_prompt_tokens=999999))


def test_runtime_generation_reserve_uses_actual_worker_allowance():
    llm = OfflineLLM(context_limit=32000)
    assert replay_budget(llm, generation_reserve=16384) == 15616
    # Smaller explicit output caps remain conservative about provider defaults.
    assert replay_budget(llm, generation_reserve=2048) == 27904
    with pytest.raises(ReplayBudgetExceeded, match="generation reserve"):
        replay_budget(llm, generation_reserve=32000)


def test_worker_generation_allowance_prevents_oversized_request():
    llm = OfflineLLM(context_limit=20000)
    messages = [{"role": "user", "content": "current instruction " * 1000}]
    # This request fits when only config.max_tokens=4096 is reserved, but not
    # when the real worker's 16384 output allowance is used.
    assert asyncio.run(bound_messages(messages, registry=registry(), llm=llm)) == messages
    with pytest.raises(ReplayBudgetExceeded, match="budget"):
        asyncio.run(bound_messages(
            messages, registry=registry(), llm=llm, generation_reserve=16384,
        ))


def test_measurement_loads_effective_replace_history_readonly(tmp_path):
    sessions = tmp_path / "sessions"
    store = SessionStore(sessions / "worker.json")
    store.save({"system_prompt": "system", "messages": history()[1:]})
    effective = [{"role": "user", "content": "only this survives"},
                 {"role": "assistant", "content": "acknowledged"}]
    store.save({"system_prompt": "system", "messages": effective}, append_from=0)
    before = {path.name: path.read_bytes() for path in sessions.iterdir() if path.is_file()}
    output = tmp_path / "measurement.json"
    result = subprocess.run([
        sys.executable, "scripts/measure_subagent_replay.py", "--sessions", str(sessions),
        "--limit", "3", "--json", str(output),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert len(report["samples"]) == 1
    assert report["samples"][0]["effective_message_count"] == 2
    assert report["samples"][0]["raw_bytes"] == sum(len(content) for content in before.values())
    assert report["semantic_validation"] == "unverified"
    assert report["decision"] == "NEEDS_SEMANTIC_VALIDATION"
    assert {path.name: path.read_bytes() for path in sessions.iterdir() if path.is_file()} == before
