"""The time of consent stays private and is not extended by persistence or audit."""

import asyncio
import ctypes
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.channels.approvals import await_chat_approval
from agent.runtime import approval_time
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.computer_policy import ComputerApprovalRequest
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from test_backend_peer_branches import backend_functions


@pytest.mark.parametrize(("platform", "expected"), [("darwin", 12.5), ("linux", 34.5), ("win32", 56.5)])
def test_clock_uses_seconds_in_the_platform_continuous_time_domain(monkeypatch, platform, expected):
    monkeypatch.setattr(approval_time, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(approval_time, "time", SimpleNamespace(
        CLOCK_MONOTONIC_RAW=1, CLOCK_BOOTTIME=2,
        clock_gettime=lambda clock: {1: 12.5, 2: 34.5}[clock], monotonic=lambda: 56.5,
    ))
    assert approval_time.continuous_now() == expected


def _timed_request(kind="computer_foreground_takeover"):
    return ComputerApprovalRequest({"kind": kind, "choices": ["once", "session", "deny"]},
                                   private_scope="local-fixture")


def _registry(request, handler, audit, grants):
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="timing_fixture", description="Fixture", parameters={"type": "object", "properties": {}},
        fn=lambda: "ok", risk="write", permission_authoritative=True, owns_session_grants=True,
        permission_check=lambda _args: request,
        permission_grant=lambda _args, original, decision: grants.append((original, decision)),
    ))
    registry.set_approval_handler(handler)
    registry.set_approval_audit_handler(audit)
    return registry


def _backend(inbox, durable_io, send, *, channel="", chat=None):
    ns = dict(approval_time=approval_time, asyncio=asyncio, logger=logging.getLogger(__name__), os=os,
              pending_tool_approvals={}, pending_yolo_approvals=set(), pending_foreground_approvals=set(),
              approval_resolutions={}, approval_inbox=inbox, durable_io=durable_io, _send=send,
              tools=SimpleNamespace(yolo=False), agent_holder={}, active_channel=SimpleNamespace(get=lambda: channel),
              active_task_id="fixture", approval_choices=lambda card: card["choices"], await_chat_approval=await_chat_approval,
              active_channel_message=SimpleNamespace(get=lambda: SimpleNamespace(channel=channel)),
              channel_manager_holder={"manager": SimpleNamespace(approver=lambda _: chat)})
    backend_functions(ns, {"request_tool_approval", "resolve_tool_approval"})
    return ns


@pytest.mark.parametrize(("channel", "choice"), [("", "session"), ("", "once"), ("weixin", "session")],
                         ids=["local-gui", "present-once", "remote-chat"])
def test_live_decision_precedes_scheduling_database_and_audit_and_never_leaks(tmp_path, monkeypatch, channel, choice):
    now, front_pid = 3.0, 111
    monkeypatch.setenv("ASTRA_UI_SURFACE", "gui")
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)
    samples = []

    def read_front():
        nonlocal now
        samples.append((now, front_pid))
        now += 2.0  # input during this lookup must not predate the decision timestamp
        return front_pid

    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    public, audit_events, stored, grants = [], [], [], []
    request = _timed_request()
    if choice == "once":
        request.update(choices=["once", "deny"], arguments={"present_result": True})

    async def delayed_io(fn, *args, **kwargs):
        nonlocal now
        if fn == inbox.resolve:
            now = 150.0
            stored.append(args[-1])
        return fn(*args, **kwargs)

    def approve(resolve):
        nonlocal now, front_pid
        assert samples == []  # the app in front while the card was prepared is not the witness
        now, front_pid = 10.0, 27182818
        assert resolve(choice)
        now, front_pid = 45.0, 333  # already a different app before resolution runs
        assert not resolve("once")

    def send(event):
        public.append(event)
        if event["type"] == "tool_approval_request" and not channel:
            approve(lambda decision: ns["resolve_tool_approval"](event["request_id"], decision))

    class Chat:
        async def ask_approval(self, _message, _prompt, answer):
            approve(answer)

        async def end_approval(self, _message, _notice):
            pass

    ns = _backend(inbox, delayed_io, send, channel=channel, chat=Chat())

    async def audit(event):
        nonlocal now
        audit_events.append(event)
        if event["type"] == "approval_decided":
            now = 200.0

    turn = _registry(request, ns["request_tool_approval"], audit, grants).execute("timing_fixture", {})
    result = asyncio.run(asyncio.wait_for(turn, timeout=2))
    assert result["error"] == "" and now == 200.0
    (original, decision), = grants
    assert original is request and decision == choice and type(decision) is str
    assert getattr(original, "private_decided_at", None) == 10.0
    assert getattr(original, "private_frontmost_pid", None) == 27182818 and samples == [(10.0, 27182818)]
    assert not ns["pending_foreground_approvals"]
    row, = inbox.list()
    assert row.state == "approved" and row.decision == choice
    for public_decision in (stored[0], public[-1]["decision"], audit_events[-1]["decision"]):
        assert type(public_decision) is str and public_decision == choice
    encoded = json.dumps([public, audit_events, row.public()])
    assert "decided_at" not in encoded and "frontmost_pid" not in encoded and "27182818" not in encoded


def test_plain_handler_is_stamped_on_return_before_audit_not_from_public_data(monkeypatch):
    now = 5.0
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)

    def read_front():
        nonlocal now
        assert now == 50.0
        now = 51.0
        return 804

    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)
    request, grants = _timed_request(), []

    async def answer(card):
        nonlocal now
        now = 50.0  # reading the card before agreeing does not consume the loan
        card["private_decided_at"] = card["arguments"]["private_decided_at"] = 10_000.0
        card["private_frontmost_pid"] = card["arguments"]["private_frontmost_pid"] = 999
        return " SESSION "

    async def audit(event):
        nonlocal now
        if event["type"] == "approval_decided":
            now = 250.0

    result = asyncio.run(_registry(request, answer, audit, grants).execute("timing_fixture", {}))
    assert result["error"] == "" and now == 250.0
    assert grants[0][0] is request and grants[0][1] == "session"
    assert getattr(request, "private_decided_at", None) == 50.0 and "private_decided_at" not in request
    assert getattr(request, "private_frontmost_pid", None) == 804 and "private_frontmost_pid" not in request


@pytest.mark.parametrize("invalid", [None, "10", True, -1.0, float("nan"), float("inf"), 51.0])
def test_invalid_receipt_does_not_fall_back_to_a_new_grant(monkeypatch, invalid):
    now = invalid
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)
    monkeypatch.setattr(approval_time, "frontmost_pid_now", lambda: 415)
    request, grants = _timed_request(), []

    async def answer(_card):
        nonlocal now
        receipt = approval_time.TimedApprovalDecision("session", foreground=True)
        now = 50.0
        return receipt

    result = asyncio.run(_registry(request, answer, None, grants).execute("timing_fixture", {}))
    assert result["error_type"] == "approval_denied" and grants == []


def test_untimed_permission_still_receives_only_the_plain_decision(monkeypatch):
    read_front = Mock(side_effect=AssertionError("untimed permissions must not inspect the desktop"))
    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)
    request, grants = {"kind": "untimed", "choices": ["once", "session", "deny"]}, []

    async def answer(_card):
        return approval_time.TimedApprovalDecision("session")

    result = asyncio.run(_registry(request, answer, None, grants).execute("timing_fixture", {}))
    assert result["error"] == ""
    (original, decision), = grants
    assert original is request and "private_decided_at" not in original
    assert decision == "session" and type(decision) is str
    read_front.assert_not_called()


@pytest.mark.parametrize(("kind", "decision"), [("computer_write", "once"), ("computer_foreground_takeover", "deny"),
                                               ("computer_foreground_takeover", "not-a-decision")])
def test_denials_and_other_requests_never_query_the_desktop(monkeypatch, kind, decision):
    read_front = Mock(side_effect=AssertionError("not a foreground approval"))
    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)

    def send(event):
        if event["type"] == "tool_approval_request":
            ns["resolve_tool_approval"](event["request_id"], decision)

    async def plain(_card):
        return decision

    ns = _backend(None, None, send)
    for handler in (plain, ns["request_tool_approval"]):
        result = asyncio.run(_registry(_timed_request(kind), handler, None, []).execute("timing_fixture", {}))
        assert (result["error"] == "") == (decision == "once")
    read_front.assert_not_called()


@pytest.mark.parametrize("pid", [None, True, 0, -1, 2**31, 3.5, "31"])
def test_missing_or_invalid_live_pid_cannot_be_replaced_by_a_later_sample(monkeypatch, pid):
    read_front = Mock(side_effect=[pid, 456])
    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)
    request = _timed_request()
    request["arguments"] = {"frontmost_pid": 456, "private_frontmost_pid": 456}

    async def answer(_card):
        return approval_time.TimedApprovalDecision("once", foreground=True)

    result = asyncio.run(_registry(request, answer, None, []).execute("timing_fixture", {}))
    assert result.get("error_type") == "approval_denied"
    assert read_front.call_count == 1


def test_automatic_answer_cannot_supply_or_refresh_a_foreground_witness(monkeypatch):
    read_front = Mock(return_value=456)
    monkeypatch.setattr(approval_time, "frontmost_pid_now", read_front)
    ns = _backend(None, None, lambda _event: None)
    ns["tools"].yolo = True

    async def automatic(card):
        # Even a misconfigured caller cannot turn an automatic answer into a human decision receipt.
        return await ns["request_tool_approval"]({**card, "yolo_bypass_allowed": True})

    result = asyncio.run(_registry(_timed_request(), automatic, None, []).execute("timing_fixture", {}))
    assert result.get("error_type") == "approval_denied"
    read_front.assert_not_called()


@pytest.fixture(autouse=True)
def isolated_loader(monkeypatch):
    # A regression must not accidentally inspect the test runner's desktop.
    load = Mock(side_effect=AssertionError("unexpected framework load"))
    monkeypatch.setattr(ctypes, "CDLL", load)
    approval_time._load_process_manager.cache_clear()
    yield load
    approval_time._load_process_manager.cache_clear()


@pytest.fixture
def framework(isolated_loader, monkeypatch):
    monkeypatch.setattr(approval_time, "sys", SimpleNamespace(platform="darwin"))
    isolated_loader.side_effect = None
    return isolated_loader, isolated_loader.return_value


def test_front_pid_queries_only_the_selected_psn_and_caches_symbols_not_the_pid(framework):
    load, library = framework

    def front(psn):
        ctypes.cast(psn, ctypes.POINTER(ctypes.c_uint32))[1] = 77
        return 0

    pids = iter((101, 2**31 - 1))

    def get_pid(psn, pid):
        assert ctypes.cast(psn, ctypes.POINTER(ctypes.c_uint32))[1] == 77
        ctypes.cast(pid, ctypes.POINTER(ctypes.c_int32))[0] = next(pids)
        return 0

    library.GetFrontProcess.side_effect = front
    library.GetProcessPID.side_effect = get_pid
    assert [approval_time.frontmost_pid_now() for _ in range(2)] == [101, 2**31 - 1]
    load.assert_called_once_with("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    assert library.GetFrontProcess.restype is ctypes.c_int16 and library.GetProcessPID.restype is ctypes.c_int32
    assert ctypes.sizeof(library.GetFrontProcess.argtypes[0]._type_) == 8
    assert library.GetProcessPID.argtypes == [*library.GetFrontProcess.argtypes, ctypes.POINTER(ctypes.c_int32)]


@pytest.mark.parametrize("failure", ["platform", "framework", "symbol", "front", "pid", "zero"])
def test_unavailable_process_manager_or_pid_returns_none(framework, monkeypatch, failure):
    load, library = framework
    if failure == "platform":
        monkeypatch.setattr(approval_time, "sys", SimpleNamespace(platform="linux"))
    elif failure == "framework":
        load.side_effect = OSError("unavailable")
    elif failure == "symbol":
        del library.GetProcessPID
    library.GetFrontProcess.return_value = -1 if failure == "front" else 0
    if failure != "symbol":
        library.GetProcessPID.return_value = -1 if failure == "pid" else 0
    assert approval_time.frontmost_pid_now() is None
    if failure == "platform":
        load.assert_not_called()
    if failure == "front":
        library.GetProcessPID.assert_not_called()
