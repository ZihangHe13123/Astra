import asyncio

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class RootLLM:
    def __init__(self):
        self.calls = 0
        self.prompts = []

    def estimate_tokens(self, messages):
        return len(str(messages)) // 4

    async def chat_stream(self, messages, tools):
        del tools
        self.calls += 1
        self.prompts.append(messages)
        content = "draft without worker" if self.calls == 1 else "final with worker evidence"
        if self.calls == 1:
            yield {"type": "reasoning", "content": "provisional reasoning"}
        yield {"type": "chunk", "content": content}
        yield {"type": "done", "content": content, "usage": None}


class CompletingMailbox:
    def __init__(self, *, running=True):
        self.running = running
        self.delivered = False
        self.result_ready = False
        self.waited = False

    def drain(self, task_id):
        del task_id
        if not self.result_ready or self.delivered:
            return []
        self.delivered = True
        return [
            "[SYSTEM-DELIVERED SUBAGENT RESULT]\n"
            "Message Type: FINAL_ANSWER\n"
            "Payload:\nverified evidence"
        ]

    def has_running(self, task_id):
        del task_id
        return self.running

    async def wait_and_drain(self, task_id, timeout=None, *, session_id=""):
        self.waited = True
        assert timeout is None
        self.running = False
        self.result_ready = True
        return self.drain(task_id)


def test_react_reconciles_running_background_mailbox_before_final_answer():
    async def scenario():
        llm = RootLLM()
        mailbox = CompletingMailbox()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
        agent.delegate_mailbox = mailbox
        msg = Msg(
            content=[ContentBlock.text("do the long task")],
            metadata={"task_id": "task-mail"},
        )

        events = [event async for event in agent.reply_stream(msg)]
        visible = "".join(
            event.get("content", "")
            for event in events
            if event.get("type") == "chunk"
        )

        assert mailbox.delivered
        assert not mailbox.running
        assert mailbox.waited
        assert llm.calls == 2
        assert visible == "draft without workerfinal with worker evidence"
        assert "verified evidence" in str(llm.prompts[1])
        assert any(event.get("type") == "reasoning" for event in events)

    asyncio.run(scenario())


def test_a_draft_answer_given_way_to_late_results_is_not_kept_in_the_saved_history(tmp_path):
    from agent.runtime.session_store import SessionStore
    from agent.runtime.task_store import TaskStore

    async def scenario():
        llm = RootLLM()
        mailbox = CompletingMailbox()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
        agent.delegate_mailbox = mailbox
        agent.context.set_session(str(tmp_path / "session.json"))
        # With a task store, as in the backend, the draft is saved as soon as it is written.
        agent.task_store = TaskStore(tmp_path / "tasks.db")
        task = agent.task_store.start_run("request", "do the long task", session_id="session")
        msg = Msg(content=[ContentBlock.text("do the long task")], metadata={"task_id": task["id"]})

        [event async for event in agent.reply_stream(msg)]
        await agent.context.save_async()

        stored = SessionStore(tmp_path / "session.json").load()["messages"]
        assert [m.get("content") for m in stored] == [m.get("content") for m in agent.context.messages]
        assert not any(m.get("content") == "draft without worker" for m in stored)
        assert stored[-1]["content"] == "final with worker evidence"

    asyncio.run(scenario())


def test_react_drains_completed_background_result_on_next_parent_turn():
    async def scenario():
        llm = RootLLM()
        mailbox = CompletingMailbox(running=False)
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
        agent.delegate_mailbox = mailbox
        first = Msg(
            content=[ContentBlock.text("start background work")],
            metadata={"task_id": "task-mail"},
        )
        [event async for event in agent.reply_stream(first)]

        mailbox.running = False
        mailbox.result_ready = True
        second = Msg(
            content=[ContentBlock.text("continue foreground work")],
            metadata={"task_id": "task-mail"},
        )
        events = [event async for event in agent.reply_stream(second)]
        visible = "".join(
            event.get("content", "")
            for event in events
            if event.get("type") == "chunk"
        )

        assert mailbox.delivered
        assert llm.calls == 2
        assert visible == "final with worker evidence"
        assert "verified evidence" in str(llm.prompts[1])
        assert any(
            "Message Type: FINAL_ANSWER" in str(message.get("content", ""))
            for message in agent.context.messages
        )

    asyncio.run(scenario())


def test_react_continues_foreground_work_after_background_delegate_dispatch():
    class DispatchLLM:
        def __init__(self):
            self.calls = 0

        def estimate_tokens(self, messages):
            return len(str(messages)) // 4

        async def chat_stream(self, messages, tools):
            del tools
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "delegate-1",
                        "name": "delegate_task",
                        "arguments": '{"goal":"research","background":true}',
                    }],
                    "content": "子代理查资料，我先处理本地部分。",
                    "reasoning_content": "",
                    "usage": None,
                }
            elif self.calls == 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "foreground-1",
                        "name": "foreground_work",
                        "arguments": "{}",
                    }],
                    "content": "并行处理主代理工作。",
                    "reasoning_content": "",
                    "usage": None,
                }
            elif self.calls == 3:
                yield {"type": "done", "content": "主代理部分已完成。", "usage": None}
            else:
                assert "verified evidence" in str(messages)
                yield {"type": "done", "content": "已综合主代理与子代理结果。", "usage": None}

    async def scenario():
        llm = DispatchLLM()
        mailbox = CompletingMailbox(running=False)
        registry = ToolRegistry()

        def dispatch(goal, background=False):
            del goal
            mailbox.running = background
            return {"process_id": "worker-1", "background": background}

        def foreground_work():
            assert mailbox.running, "foreground work must overlap the background worker"
            return "foreground complete"

        registry.register(ToolDef(
            "delegate_task",
            "delegate",
            {"type": "object"},
            dispatch,
        ))
        registry.register(ToolDef(
            "foreground_work",
            "foreground",
            {"type": "object"},
            foreground_work,
        ))
        agent = ReActAgent("agent", llm, registry, max_iterations=3)
        agent.delegate_mailbox = mailbox
        events = [event async for event in agent.reply_stream(Msg(
            content=[ContentBlock.text("parallelize it")],
            metadata={"task_id": "task-dispatch"},
        ))]

        assert llm.calls == 4
        assert mailbox.waited
        assert mailbox.delivered
        assert any(event.get("type") == "done" for event in events)
        assert agent.context.messages[-1]["content"] == "已综合主代理与子代理结果。"
        assert any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "delegate-1"
            for message in agent.context.messages
        )

    asyncio.run(scenario())
