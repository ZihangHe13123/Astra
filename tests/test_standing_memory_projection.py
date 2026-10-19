"""Core memory reaches the model once per change, not inside every turn snapshot."""

import asyncio

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.runtime_context_projection import STANDING_MEMORY_MARKER, TURN_CONTEXT_MARKER
from agent.runtime.tools.registry import ToolRegistry

CORE = "Prefers answers that cite the file and line"
ADDED = "Works mostly in the astra-master checkout"


class RecordingLLM:
    class config:
        model = "fake"
        capabilities = frozenset()

    def __init__(self):
        self.requests: list[list[dict]] = []

    @staticmethod
    def estimate_tokens(value):
        return len(str(value)) // 4

    async def chat_stream(self, messages, tools):
        del tools
        self.requests.append(messages)
        yield {"type": "chunk", "content": "ok"}
        yield {"type": "done", "content": "ok", "usage": None}


def _agent(tmp_path, llm, session="session.json"):
    store = MemoryStore(tmp_path / "memory.db")
    agent = ReActAgent("agent", llm, ToolRegistry(), system_prompt="base prompt", memory_store=store)
    agent.context.set_session(str(tmp_path / session))
    return agent, store


def _turn(agent, text):
    async def run():
        async for _ in agent.reply_stream(Msg(content=[ContentBlock.text(text)])):
            pass
    asyncio.run(run())


def _text(message):
    content = message.get("content")
    return content if isinstance(content, str) else str(content)


def test_core_memory_is_sent_once_while_the_turn_context_changes(tmp_path):
    llm = RecordingLLM()
    agent, store = _agent(tmp_path, llm)
    store.add_core("user", CORE)
    for index in range(4):
        # A different conversation state each turn changes the turn snapshot.
        store.update_working(agent.context.session_scope, "temporary_constraints", f"constraint {index}")
        _turn(agent, f"question {index}")

    last = llm.requests[-1]
    assert sum(_text(message).count(CORE) for message in last) == 1
    standing = [m for m in last if _text(m).startswith(STANDING_MEMORY_MARKER)]
    turns = [m for m in last if _text(m).startswith(TURN_CONTEXT_MARKER)]
    assert len(standing) == 1 and CORE in _text(standing[0])
    assert len(turns) == 4 and all(CORE not in _text(m) for m in turns)
    # Each request extends the one before it, so the cached prefix survives.
    for earlier, later in zip(llm.requests, llm.requests[1:]):
        assert later[:len(earlier)] == earlier


def test_a_changed_core_file_appends_one_complete_copy_on_the_next_turn(tmp_path):
    llm = RecordingLLM()
    agent, store = _agent(tmp_path, llm)
    store.add_core("user", CORE)
    _turn(agent, "first")
    _turn(agent, "second")
    store.add_core("memory", ADDED)
    _turn(agent, "third")

    before, after = llm.requests[-2], llm.requests[-1]
    assert after[:len(before)] == before
    standing = [_text(m) for m in after if _text(m).startswith(STANDING_MEMORY_MARKER)]
    assert len(standing) == 2
    assert ADDED not in standing[0] and ADDED in standing[1] and CORE in standing[1]
    assert "supersedes earlier SYSTEM-SUPPLIED STANDING MEMORY copies" in standing[1]


def test_a_reloaded_session_does_not_send_core_memory_again(tmp_path):
    llm = RecordingLLM()
    agent, store = _agent(tmp_path, llm)
    store.add_core("user", CORE)
    _turn(agent, "first")
    agent.context.save()

    resumed_llm = RecordingLLM()
    resumed, _ = _agent(tmp_path, resumed_llm)
    assert resumed.context.load()
    _turn(resumed, "after restart")
    request = resumed_llm.requests[-1]
    assert sum(_text(message).count(CORE) for message in request) == 1


def test_compaction_keeps_only_the_latest_core_memory_copy():
    context = AgentContext(system_prompt="system")
    context.add_user("first")
    context.get_prompt(runtime_context="turn one", standing_memory="core v1")
    context.add_assistant("answer")
    context.add_user("second")
    prompt = context.get_prompt(runtime_context="turn two", standing_memory="core v2")
    assert [m["content"].count("core v") for m in prompt if m["content"].startswith(STANDING_MEMORY_MARKER)] == [1, 1]
    # Compaction retires superseded copies in both streams.
    assert context.memory_projection.compact(context.messages)
    assert context.runtime_projection.compact(context.messages)
    compacted = context.get_prompt()
    copies = [m["content"] for m in compacted if m["content"].startswith(STANDING_MEMORY_MARKER)]
    assert len(copies) == 1 and "core v2" in copies[0]
    # The standing copy comes before the turn context at the same position.
    kinds = [m["content"][:30] for m in compacted if m["role"] == "user" and m["content"].startswith("[SYSTEM")]
    assert kinds[0].startswith(STANDING_MEMORY_MARKER[:30]) and kinds[1].startswith(TURN_CONTEXT_MARKER[:30])


def test_clearing_core_memory_says_earlier_copies_no_longer_apply():
    context = AgentContext(system_prompt="system")
    context.add_user("first")
    context.get_prompt(standing_memory="core v1")
    context.add_assistant("answer")
    context.add_user("second")
    prompt = context.get_prompt(standing_memory="")
    assert "Core memory: none. Earlier copies no longer apply." in prompt[-1]["content"]
