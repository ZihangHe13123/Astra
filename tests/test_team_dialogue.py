"""A Team member is reused for the whole conversation, and lead and member can talk while it works."""

import asyncio
import copy
import json
import re

import pytest

from agent.runtime.agent_team import TEAM_MESSAGE_CHARS, AgentTeamStore
from agent.runtime.tools.registry import ToolDef
from test_delegate_conversation_resume import Harness, RecordingLLM


def _envelopes(request):
    return [m for m in request if str(m.get("content") or "").startswith("[SYSTEM-DELIVERED TEAM MESSAGE")]


async def _wait_status(harness, agent_id, *statuses):
    for _ in range(500):
        agent = harness.teams.get_agent(agent_id) or {}
        if agent.get("status") in statuses:
            return agent
        await asyncio.sleep(0.01)
    raise AssertionError(harness.teams.get_agent(agent_id))


async def _wait_requests(llm, count):
    for _ in range(500):
        if len(llm.requests) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{len(llm.requests)} requests, expected {count}")


@pytest.mark.parametrize("how", ["reported", "idle_timeout", "cancelled_while_idle", "backend_interrupted"])
def test_a_message_from_the_lead_picks_up_a_stopped_member_where_it_left_off(tmp_path, monkeypatch, how):
    monkeypatch.setenv("ASTRA_TEAM_IDLE_TIMEOUT_SECONDS", "1" if how == "idle_timeout" else "60")

    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(keep_alive=how != "reported")
        agent_id = spawned["agent"]["id"]
        if how == "reported":
            await harness.wait(spawned["process"]["process_id"])
        elif how == "idle_timeout":
            await harness.idle(agent_id)
            await harness.wait(spawned["process"]["process_id"])
        else:
            await harness.idle(agent_id)
            await harness.cancel(spawned["process"]["process_id"])
            if how == "backend_interrupted":
                # What a new backend records for a member the old one left behind.
                harness.teams.set_agent_status(agent_id, "interrupted")
        assert len(llm.requests) == 1
        stopped = await _wait_status(harness, agent_id, "completed", "cancelled", "interrupted")

        sent = await harness.call(
            "team_send", team_id=team["id"], to="worker", kind="task_assignment",
            message="Second task: check the other module",
        )

        assert sent["sent"] == 1 and sent["resumed"]["agent_id"] == agent_id
        await _wait_requests(llm, 2)
        first, second = llm.requests
        # Same system prompt and history, then the lead's message: nothing added in between,
        # so the request starts exactly as the last one did.
        assert second[:len(first)] == first
        assert second[len(first)]["role"] == "assistant" and second[len(first)]["content"] == "Report 1"
        assert len(_envelopes(second)) == 1 and "Second task" in _envelopes(second)[0]["content"]
        assert second[-1] is not None and "Resume from the saved state" not in json.dumps(second)
        assert stopped["status"] != "running"
        if how != "reported":
            await harness.idle(agent_id, calls=2)
            await harness.cancel(sent["resumed"]["process_id"])
        else:
            await harness.wait(sent["resumed"]["process_id"])

    asyncio.run(scenario())


def test_a_member_stopped_mid_step_is_not_picked_up_by_a_message(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        llm = RecordingLLM([{"content": "", "tool_calls": [{"id": "write-1", "name": "write_file", "arguments": "{}"}]}])
        harness = Harness(tmp_path, monkeypatch, llm)

        async def write_file():
            started.set()
            await asyncio.Event().wait()

        harness.registry.register(ToolDef("write_file", "Write", {"type": "object", "properties": {}}, write_file,
                                          group="files", risk="write", approval="never"))
        team, spawned = await harness.spawn(mode="worker")
        await asyncio.wait_for(started.wait(), 5)
        await harness.cancel(spawned["process"]["process_id"])
        await _wait_status(harness, spawned["agent"]["id"], "cancelled")

        refused = await harness.registry.execute(
            "team_send", {"team_id": team["id"], "to": "worker", "message": "carry on"}, task_id=harness.parent,
        )

        # The write may have happened: carrying on as if nothing were in doubt is not offered.
        assert "outcome" in refused["error"] and "unknown" in refused["error"]
        assert "team_restart" in refused["error"]
        assert len(llm.requests) == 1

    asyncio.run(scenario())


class AskingLLM:
    """Asks the lead a question, then keeps working until released, then reports."""

    def __init__(self):
        self.requests = []
        self.release = asyncio.Event()

    async def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        if len(self.requests) == 1:
            team_id = re.search(r"in team (\S+)\.", kwargs["messages"][0]["content"]).group(1)
            return {"content": "", "tool_calls": [{"id": "ask", "name": "team_send", "arguments": json.dumps(
                {"team_id": team_id, "to": "lead", "message": "Use plan A or B?"})}]}
        if len(self.requests) == 2:
            await self.release.wait()
        return {"content": f"Report {len(self.requests)}", "tool_calls": []}


def test_what_a_member_writes_to_the_lead_reaches_the_parent_once_and_wakes_its_wait(tmp_path, monkeypatch):
    async def scenario():
        llm = AskingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(keep_alive=True)
        session = "session-a"
        # The parent waits for its running member and is woken by the question, not by an ending.
        envelopes = await asyncio.wait_for(
            harness.mailbox.wait_and_drain(harness.parent, timeout=None, session_id=session), 5,
        )
        assert len(envelopes) == 1
        assert "Use plan A or B?" in envelopes[0] and "From: worker" in envelopes[0]
        assert (harness.teams.get_agent(spawned["agent"]["id"]) or {}).get("status") == "running"
        # Taken once: neither the next drain nor the inbox hands it over again.
        assert harness.mailbox.drain_for(harness.parent, session) == []
        inbox = await harness.call("team_inbox", team_id=team["id"])
        assert inbox["messages"] == []
        llm.release.set()
        await harness.idle(spawned["agent"]["id"], calls=2)
        await harness.cancel(spawned["process"]["process_id"])

    asyncio.run(scenario())


def test_a_message_can_hand_over_a_whole_assignment(tmp_path):
    store = AgentTeamStore(tmp_path / "team.db")
    team = store.create_team("owner", session_id="session", name="long", goal="test")
    agent = store.register_agent(team["id"], name="worker", role="worker", mode="worker",
                                 parent_agent_id=team["lead_agent_id"])
    store.send_message(team["id"], team["lead_agent_id"], agent["id"], body="x" * TEAM_MESSAGE_CHARS)
    with pytest.raises(ValueError, match=str(TEAM_MESSAGE_CHARS)):
        store.send_message(team["id"], team["lead_agent_id"], agent["id"], body="x" * (TEAM_MESSAGE_CHARS + 1))
    assert TEAM_MESSAGE_CHARS == 16_000
