"""Execute the actual backend closures with deterministic inbox interleavings."""
import ast
import asyncio
import logging
import os  # noqa: F401 - globals used by the executed backend closures
import time
from contextlib import suppress  # noqa: F401 - globals used by the executed backend closures
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli.images import build_user_message_content  # noqa: F401 - globals used by the executed backend closures
from agent.core.msg import Msg
from agent.runtime.async_io import durable_io
from agent.runtime.conversation_branches import ConversationBranches, branch_scope
from agent.runtime.peer_link import HEARTBEAT_SECONDS, PeerError, PeerLink, default_name, fallback_name, incoming_prompt  # noqa: F401 - globals used by the executed backend closures


def backend_functions(namespace, names):
    tree = ast.parse((Path(__file__).parents[1] / "agent/cli/backend.py").read_text())
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_main")
    nodes = [node for node in main.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    # The harness supplies captured variables as globals; execute the production
    # bodies rather than copying the race-sensitive checks into this test.
    for node in nodes:
        node.body = [item for item in node.body if not isinstance(item, ast.Nonlocal)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "backend_closures", "exec"), namespace)


def harness(tmp_path):
    receiver = PeerLink(tmp_path / "peers.db", site="test")
    receiver.join("conversation", "receiver")
    sender = PeerLink(receiver.path, site="test")
    sender.join("other", "sender")
    context = SimpleNamespace(session_scope="conversation", session_path=str(tmp_path / "conversation.json"), branch_id="main", messages=[])
    idle = {"busy": False}
    launched, events = [], []
    ns = dict(globals(), agent=SimpleNamespace(context=context, llm=SimpleNamespace(config=SimpleNamespace(connection_required=False))),
              peer_link=receiver, peer_lock=asyncio.Lock(), peer_clock={"heartbeat": time.monotonic(), "inbox": 0.0},
              _turn_busy=lambda: idle["busy"], _response_regenerating=False, restart=SimpleNamespace(draining=False),
              active_channel=SimpleNamespace(get=lambda: None), bar_mode=SimpleNamespace(active=False),
              minimal_mode=SimpleNamespace(active=False), local_mode=SimpleNamespace(active=False),
              _first_request=lambda messages: "", _peer_event=lambda *args: events.append(args),
              _start_message=lambda msg, text: launched.append(msg) or True, logger=logging.getLogger(__name__),
              _send=events.append, pending_tool_approvals={}, approval_resolutions={},
              question_broker=SimpleNamespace(pending_count=0, queued_request_ids=[]),
              wakeups=SimpleNamespace(status=lambda: {"state": "idle"}), process_manager=SimpleNamespace(list=lambda **kw: []),
              delegate_mailbox=SimpleNamespace(manager=SimpleNamespace(list=lambda **kw: [])), delegate_statuses={})
    backend_functions(ns, {"_peer", "_peer_sync_locked", "_peer_tick", "_response_guard", "_response_head_current"})
    return ns, receiver, sender, context, idle, launched


def test_claimed_mail_is_released_to_its_old_branch_before_identity_changes(tmp_path):
    async def run():
        ns, receiver, sender, context, idle, launched = harness(tmp_path)
        sender.send("mail for old branch", to=receiver.peer_id)
        claimed, resume = asyncio.Event(), asyncio.Event()
        async def gated(fn, *args, **kwargs):
            result = await durable_io(fn, *args, **kwargs)
            if fn.__name__ == "claim":
                claimed.set()
                await resume.wait()
            return result
        ns["durable_io"] = gated
        tick = asyncio.create_task(ns["_peer_tick"]())
        await asyncio.wait_for(claimed.wait(), 2)
        context.session_scope = branch_scope(context.session_path, "a" * 32)
        async def rebind():
            async with ns["peer_lock"]:
                await ns["_peer_sync_locked"]()
        changed = asyncio.create_task(rebind())
        await asyncio.sleep(0)
        assert not changed.done() and receiver.peer_id == "test:conversation"
        with pytest.raises(ValueError):
            ns["_peer"]()  # tools cannot borrow the stale peer identity
        resume.set()
        await asyncio.wait_for(asyncio.gather(tick, changed), 2)
        assert launched == []
        assert receiver.peer_id == f"test:{context.session_scope}" and not receiver.has_mail()
        old = PeerLink(receiver.path, site="test")
        old.join("conversation", "receiver")
        assert old.has_mail()
        context.session_scope = "conversation"
        ns["peer_clock"]["inbox"] = 0
        await ns["_peer_tick"]()
        assert len(launched) == 1 and not old.has_mail()
    asyncio.run(run())


def test_work_started_during_claim_prevents_admission_without_losing_mail(tmp_path):
    async def run():
        ns, receiver, sender, context, idle, launched = harness(tmp_path)
        sender.send("later", to=receiver.peer_id)
        async def changed_busy(fn, *args, **kwargs):
            result = await durable_io(fn, *args, **kwargs)
            if fn.__name__ == "claim":
                idle["busy"] = True
            return result
        ns["durable_io"] = changed_busy
        await ns["_peer_tick"]()
        assert launched == [] and receiver.has_mail()
    asyncio.run(run())


def test_response_guard_accepts_question_property_and_rejects_open_peer_work(tmp_path):
    async def run():
        ns, receiver, sender, context, idle, launched = harness(tmp_path)
        await ns["_response_guard"]()  # [] is a property value, not callable
        task = receiver.send("work", to=sender.peer_id)
        with pytest.raises(ValueError, match="peer tasks"):
            await ns["_response_guard"]()
        sender.claim_inbox()
        sender.update(task["task_id"], "completed", "late reply")
        assert receiver.tasks() == [] and receiver.has_mail()
        with pytest.raises(ValueError, match="peer tasks"):
            await ns["_response_guard"]()
        await ns["_peer_tick"]()
        await ns["_response_guard"]()
    asyncio.run(run())


def test_direct_lifecycle_start_cannot_overwrite_regeneration_task(tmp_path):
    ns, *_ = harness(tmp_path)
    backend_functions(ns, {"_start_message"})
    marker = object()
    ns.update(_response_regenerating=True, active_task=marker, active_task_id="regenerate")
    assert ns["_start_message"](Msg(sender="peer", role="user", content=[]), "old mail") is False
    assert ns["active_task"] is marker and ns["active_task_id"] == "regenerate"


def test_gui_admission_fails_closed_when_published_head_is_unreadable_or_different(tmp_path, monkeypatch):
    ns, _, _, context, _, _ = harness(tmp_path)
    monkeypatch.setenv("ASTRA_UI_SURFACE", "gui")
    assert ns["_response_head_current"]()
    context.branch_id = "a" * 32
    assert not ns["_response_head_current"]()
    context.branch_id = "main"
    branches = ConversationBranches(context.session_path)
    branches.manifest_path.parent.mkdir(parents=True)
    branches.manifest_path.write_text("broken")
    assert not ns["_response_head_current"]()
    assert branches.manifest_path.read_text() == "broken"
