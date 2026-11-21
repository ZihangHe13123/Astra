"""The time of consent stays private and is not extended by persistence or audit."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

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


def _timed_request():
    return ComputerApprovalRequest({"kind": "computer_foreground_takeover", "choices": ["once", "session", "deny"]},
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


def test_live_decision_precedes_scheduling_database_and_audit_and_never_leaks(tmp_path, monkeypatch):
    now = 10.0
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    public, audit_events, stored, grants = [], [], [], []
    request = _timed_request()

    async def delayed_io(fn, request_id, decision):
        nonlocal now
        now = 150.0
        stored.append(decision)
        return fn(request_id, decision)

    pending = {}
    ns = dict(approval_time=approval_time, asyncio=asyncio, logger=logging.getLogger(__name__),
              pending_tool_approvals=pending, approval_resolutions={}, approval_inbox=inbox,
              durable_io=delayed_io, _send=public.append)
    backend_functions(ns, {"resolve_tool_approval"})

    async def answer(card):
        nonlocal now
        request_id = inbox.create(card).request_id
        public.append(card)
        waiting = pending[request_id] = asyncio.get_running_loop().create_future()
        assert ns["resolve_tool_approval"](request_id, "session")
        now = 45.0  # before the resolution coroutine gets any CPU
        assert not ns["resolve_tool_approval"](request_id, "once")
        return await waiting

    async def audit(event):
        nonlocal now
        audit_events.append(event)
        if event["type"] == "approval_decided":
            now = 200.0

    result = asyncio.run(_registry(request, answer, audit, grants).execute("timing_fixture", {}))
    assert result["error"] == "" and now == 200.0
    (original, decision), = grants
    assert original is request and decision == "session" and type(decision) is str
    assert getattr(original, "private_decided_at", None) == 10.0
    row, = inbox.list()
    assert row.state == "approved" and row.decision == "session"
    for public_decision in (stored[0], public[-1]["decision"], audit_events[-1]["decision"]):
        assert type(public_decision) is str and public_decision == "session"
    assert "decided_at" not in json.dumps([public, audit_events, row.public()])


def test_plain_handler_is_stamped_on_return_before_audit_not_from_public_data(monkeypatch):
    now = 5.0
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)
    request, grants = _timed_request(), []

    async def answer(card):
        nonlocal now
        now = 50.0  # reading the card before agreeing does not consume the loan
        card["private_decided_at"] = card["arguments"]["private_decided_at"] = 10_000.0
        return " SESSION "

    async def audit(event):
        nonlocal now
        if event["type"] == "approval_decided":
            now = 250.0

    result = asyncio.run(_registry(request, answer, audit, grants).execute("timing_fixture", {}))
    assert result["error"] == "" and now == 250.0
    assert grants[0][0] is request and grants[0][1] == "session"
    assert getattr(request, "private_decided_at", None) == 50.0 and "private_decided_at" not in request


@pytest.mark.parametrize("invalid", [None, "10", True, -1.0, float("nan"), float("inf"), 51.0])
def test_invalid_receipt_does_not_fall_back_to_a_new_grant(monkeypatch, invalid):
    now = invalid
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now)
    request, grants = _timed_request(), []

    async def answer(_card):
        nonlocal now
        receipt = approval_time.TimedApprovalDecision("session")
        now = 50.0
        return receipt

    result = asyncio.run(_registry(request, answer, None, grants).execute("timing_fixture", {}))
    assert result["error_type"] == "approval_denied" and grants == []


def test_untimed_permission_still_receives_only_the_plain_decision():
    request, grants = {"kind": "untimed", "choices": ["once", "session", "deny"]}, []

    async def answer(_card):
        return approval_time.TimedApprovalDecision("session")

    result = asyncio.run(_registry(request, answer, None, grants).execute("timing_fixture", {}))
    assert result["error"] == ""
    (original, decision), = grants
    assert original is request and "private_decided_at" not in original
    assert decision == "session" and type(decision) is str
