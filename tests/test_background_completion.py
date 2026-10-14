"""Ended background runs and subagent reports waiting for the agent that started them."""

import asyncio
import json

import pytest

from agent.runtime.background_completion import BackgroundCompletions, wake_display
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import DelegateMailbox, DelegateMailboxStore, register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def run(manager: ProcessManager, output: str, exit_code: int = 0):
    async def factory(on_output):
        on_output("stdout", output)
        return {"output": output, "error": "", "exit_code": exit_code}

    process = manager.start(factory, kind="shell", label=f"echo {output[:20]}")
    manager.expose(process)
    await manager.wait(process, 2000)
    return process


def test_runs_that_end_together_are_one_delivery_once_they_settle(tmp_path):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        clock = Clock()
        completions = BackgroundCompletions(manager, clock=clock, wake_limit=8)
        first = await run(manager, "first-ok")
        second = await run(manager, "second-bad", exit_code=2)
        for process in (first, second):
            completions.watch(process.process_id, "session", "turn")

        waiting = completions.pending("session")
        assert waiting == [first.label, second.label]
        assert not completions.ready(bool(waiting))
        clock.now += 1.0
        assert completions.ready(bool(completions.pending("session")))

        delivered = completions.drain("session")
        assert [display for _, display in delivered] == [
            "后台命令结束：echo first-ok（完成）", "后台命令结束：echo second-bad（失败，退出码 2）"]
        assert "first-ok" in delivered[0][0] and "exit code 0" in delivered[0][0]
        assert "second-bad" in delivered[1][0] and "failed, exit code 2" in delivered[1][0]
        assert completions.pending("session") == [] and completions.drain("session") == []
        assert not completions.ready(False)

    asyncio.run(scenario())


def test_a_run_whose_end_a_tool_result_showed_is_not_delivered(tmp_path):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        completions = BackgroundCompletions(manager)
        process = await run(manager, "seen")
        completions.watch(process.process_id, "session")
        manager.mark_reported(manager.describe(process))
        assert completions.pending("session") == []
        assert completions.drain("session") == []

    asyncio.run(scenario())


def test_long_output_keeps_its_end_and_points_to_the_full_log(tmp_path):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        completions = BackgroundCompletions(manager)
        process = await run(manager, "x" * 9000 + "TAIL-MARK")
        completions.watch(process.process_id, "session")
        [(notice, _)] = completions.drain("session")
        assert "Output (end):" in notice and notice.count("x") < 3100
        assert "TAIL-MARK" in notice
        assert f'process_read {{"process_id": "{process.process_id}"}}' in notice

    asyncio.run(scenario())


def test_a_stopped_turns_runs_wait_for_the_user_and_other_sessions_keep_theirs(tmp_path):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        completions = BackgroundCompletions(manager)
        stopped = await run(manager, "stopped-turn")
        other = await run(manager, "other-session")
        completions.watch(stopped.process_id, "session", "stopped")
        completions.watch(other.process_id, "elsewhere", "turn")
        completions.quiet_task("stopped")

        # Nothing starts a turn for the stopped one, but the next turn still gets it.
        assert completions.pending("session") == []
        assert [d for _, d in completions.drain("session")] == ["后台命令结束：echo stopped-turn（完成）"]
        assert completions.pending("elsewhere") == [other.label]
        completions.forget("elsewhere")
        assert completions.drain("elsewhere") == []

    asyncio.run(scenario())


def test_automatic_turns_stop_at_the_limit_until_the_user_writes(tmp_path):
    completions = BackgroundCompletions(ProcessManager(artifact_dir=tmp_path / "processes"),
                                        clock=Clock(), wake_limit=2, settle_seconds=0)
    for _ in range(2):
        assert completions.ready(True)
        completions.started_turn()
    assert completions.limited and not completions.ready(True)
    completions.user_turn()
    assert completions.ready(True)


def test_the_wake_line_names_what_ended():
    labels = [f"job {index}" for index in range(6)]
    assert wake_display(labels) == "后台任务完成，继续处理：job 0；job 1；job 2；job 3 等 6 项"
    assert wake_display(["x" * 200]).endswith("…")


class Worker:
    """A subagent that reports when released."""

    def __init__(self):
        self.release = asyncio.Event()

    async def chat(self, **kwargs):
        await self.release.wait()
        return {"content": "survey: three notes", "tool_calls": []}


async def call(registry, task_id, tool, **args):
    result = await registry.execute(tool, args, task_id=task_id)
    assert not result["error"], result["error"]
    return json.loads(result["output"])


def test_a_later_turn_of_the_session_reaches_and_settles_an_earlier_turns_subagent(tmp_path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        tasks = TaskStore(tmp_path / "tasks.db")
        session = {"id": "session"}
        worker = Worker()
        registry = ToolRegistry()
        mailbox = register_delegate_tools(registry, llm_getter=lambda: worker, task_store=tasks,
                                          session_id_getter=lambda: session["id"])
        mailbox.wakes_parent = lambda task_id: task_id == "first"
        started = await call(registry, "first", "delegate_task", goal="Survey notes", mode="explorer",
                             background=True)
        assert "You need not wait for it" in started["message"]
        process_id = started["process_id"]

        # The first turn answered; the next one asks after the subagent.
        polled = await call(registry, "second", "delegate_poll", process_id=process_id)
        assert polled["status"] == "running"
        assert process_id in {item["process_id"] for item in await call(registry, "second", "delegate_list")}
        session["id"] = "other"
        denied = await registry.execute("delegate_poll", {"process_id": process_id}, task_id="third")
        assert "another task" in denied["error"]
        session["id"] = "session"

        worker.release.set()
        await manager.wait(manager.get(process_id), 3000)
        await asyncio.sleep(0)
        assert mailbox.undelivered("session") == ["Survey notes"]
        assert mailbox.undelivered("other") == []
        polled = await call(registry, "second", "delegate_poll", process_id=process_id)
        assert "survey: three notes" in json.dumps(polled)
        # Seen through the poll, the report is not put before the parent again.
        assert mailbox.undelivered("session") == []
        assert mailbox.drain_session("session") == []

    asyncio.run(scenario())


def test_an_idle_parent_gets_every_report_of_its_session_once(tmp_path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        worker = Worker()
        worker.release.set()
        registry = ToolRegistry()
        mailbox = register_delegate_tools(registry, llm_getter=lambda: worker,
                                          task_store=TaskStore(tmp_path / "tasks.db"),
                                          session_id_getter=lambda: "session")
        ids = [(await call(registry, turn, "delegate_task", goal=f"Goal {turn}", mode="explorer",
                           background=True))["process_id"] for turn in ("first", "second")]
        for process_id in ids:
            await manager.wait(manager.get(process_id), 3000)
        await asyncio.sleep(0)
        assert sorted(mailbox.undelivered("session")) == ["Goal first", "Goal second"]
        reports = mailbox.drain_session("session")
        assert len(reports) == 2 and all("survey: three notes" in report for report in reports)
        assert mailbox.drain_session("session") == [] and mailbox.drain_for("third", "session") == []

    asyncio.run(scenario())


def test_reports_left_by_an_earlier_backend_wait_for_the_next_turn(tmp_path):
    store = DelegateMailboxStore(tmp_path / "tasks.db")
    store.upsert("old-turn", {"process_id": "gone", "session_id": "session", "goal": "Earlier survey",
                              "status": "completed", "payload": {"result": "left behind"}})
    mailbox = DelegateMailbox(ProcessManager(artifact_dir=tmp_path / "processes"), store)
    assert mailbox.undelivered("session") == []
    assert not mailbox.lead_waiting("session")
    [report] = mailbox.drain_for("new-turn", "session")
    assert "left behind" in report


@pytest.mark.parametrize("wakes", [True, False])
def test_the_dispatch_result_says_whether_the_parent_may_answer_first(tmp_path, monkeypatch, wakes):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        worker = Worker()
        registry = ToolRegistry()
        mailbox = register_delegate_tools(registry, llm_getter=lambda: worker,
                                          task_store=TaskStore(tmp_path / "tasks.db"),
                                          session_id_getter=lambda: "session")
        mailbox.wakes_parent = lambda task_id: wakes
        started = await call(registry, "turn", "delegate_task", goal="Survey", mode="explorer", background=True)
        assert ("You need not wait for it" in started["message"]) is wakes
        worker.release.set()
        await manager.wait(manager.get(started["process_id"]), 3000)

    asyncio.run(scenario())
