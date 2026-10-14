"""A subagent compacts its own history the way the main agent does, and keeps the result."""

import asyncio
import copy
import json
from types import SimpleNamespace

from agent.runtime.context_compressor import SUMMARY_PREFIX
from agent.runtime.tools.registry import ToolDef
from test_delegate_conversation_resume import Harness, _load_closed

READS = 12


class LongWorkLLM:
    """Reads one part per turn, then reports; answers the compressor's summary requests."""

    def __init__(self, reads=READS):
        self.reads = reads
        self.requests = []
        self.summaries = []
        self.config = SimpleNamespace(max_tokens=4096, context_limit=200_000, model="fixture", provider="")

    async def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        turn = len(self.requests)
        if turn <= self.reads:
            return {"content": "", "tool_calls": [
                {"id": f"read-{turn}", "name": "read_file", "arguments": json.dumps({"path": f"part-{turn}.txt"})},
            ]}
        return {"content": "Final report over every part", "tool_calls": []}

    async def chat_limited(self, messages=None, *args, **kwargs):
        del args, kwargs
        self.summaries.append(messages)
        return {"content": f"Parts read so far, summary {len(self.summaries)}.", "tool_calls": []}


def _register_parts(harness):
    def read_file(path=""):
        # Distinct text per part, about a thousand tokens each.
        return f"{path}: " + " ".join(f"{path}-word-{index}" for index in range(200))

    harness.registry.register(ToolDef(
        "read_file", "Read a part", {"type": "object", "properties": {"path": {"type": "string"}}},
        read_file, risk="read",
    ))


def _summary_messages(messages):
    return [message for message in messages if str(message.get("content") or "").startswith(SUMMARY_PREFIX)]


def test_a_teammate_compacts_its_history_keeps_the_summary_and_saves_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", "8000")

    async def scenario():
        llm = LongWorkLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        _register_parts(harness)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])

        polled = await harness.call("delegate_poll", process_id=spawned["process"]["process_id"])
        assert polled["result"]["result"] == "Final report over every part"
        # The history was compacted once it outgrew the budget, and the summary was then kept:
        # updated when it grew again, not written anew for every request that followed.
        compacted = [index for index, request in enumerate(llm.requests) if _summary_messages(request)]
        assert compacted, "no request carried a compacted history"
        assert 1 <= len(llm.summaries) < len(llm.requests) - compacted[0]
        # Each request after the first compaction carries exactly one summary, in the history itself.
        assert all(len(_summary_messages(llm.requests[index])) == 1 for index in compacted)
        # The newest evidence is never what gets summarised away.
        last = json.dumps(llm.requests[-1], ensure_ascii=False)
        assert f"part-{READS}.txt-word-199" in last
        assert "part-1.txt-word-199" not in last

        # What was saved is the compacted history, so a restart starts from it.
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        saved = _load_closed(agent["conv_path"])["messages"]
        assert len(_summary_messages(saved)) == 1
        assert len(saved) < 2 + 2 * READS
        assert team["id"]

    asyncio.run(scenario())


def test_a_one_shot_delegate_without_saved_history_compacts_too(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", "8000")

    async def scenario():
        llm = LongWorkLLM()
        harness = Harness(tmp_path, monkeypatch, llm, persist=False)
        _register_parts(harness)
        result = await harness.call("delegate_task", goal="read every part", timeout=60)

        assert result["result"] == "Final report over every part"
        assert any(_summary_messages(request) for request in llm.requests)
        assert len(llm.summaries) < len(llm.requests)

    asyncio.run(scenario())


def test_a_history_within_its_budget_is_left_whole(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", raising=False)

    async def scenario():
        llm = LongWorkLLM(reads=3)
        harness = Harness(tmp_path, monkeypatch, llm)
        _register_parts(harness)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])

        assert llm.summaries == []
        assert not any(_summary_messages(request) for request in llm.requests)
        assert team["id"]

    asyncio.run(scenario())


def test_an_earlier_write_is_summarised_like_the_main_agent_and_never_repeated(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", "8000")

    class WriteFirstLLM(LongWorkLLM):
        async def chat(self, **kwargs):
            if not self.requests:
                self.requests.append(copy.deepcopy(kwargs["messages"]))
                return {"content": "", "tool_calls": [
                    {"id": "write-1", "name": "write_file", "arguments": json.dumps({"text": "NOTE-WRITTEN-ONCE"})},
                ]}
            return await super().chat(**kwargs)

    async def scenario():
        llm = WriteFirstLLM(reads=READS + 1)
        harness = Harness(tmp_path, monkeypatch, llm)
        _register_parts(harness)
        written = []
        harness.registry.register(ToolDef(
            "write_file", "Write a note", {"type": "object", "properties": {"text": {"type": "string"}}},
            lambda text="": written.append(text) or "stored NOTE-WRITTEN-ONCE",
            group="files", risk="write", approval="never",
        ))
        team, spawned = await harness.spawn(mode="worker")
        await harness.wait(spawned["process"]["process_id"])

        assert written == ["NOTE-WRITTEN-ONCE"]
        # While it was recent the write stayed as it was.
        assert "stored NOTE-WRITTEN-ONCE" in json.dumps(llm.requests[2], ensure_ascii=False)
        # Later it went into the summary with the rest of the early work: the summariser was
        # shown it, so the record of what was done is the summary's, as for the main agent.
        assert any(_summary_messages(request) for request in llm.requests)
        assert any("NOTE-WRITTEN-ONCE" in json.dumps(summary, ensure_ascii=False) for summary in llm.summaries)
        assert "stored NOTE-WRITTEN-ONCE" not in json.dumps(llm.requests[-1], ensure_ascii=False)
        assert team["id"]

    asyncio.run(scenario())
