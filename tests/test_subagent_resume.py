import json

import pytest

from agent.runtime.subagent_conversation import ConversationCorrupt
from agent.runtime.subagent_resume import persistent_messages, recovery_tool_risk, restore_conversation
from agent.runtime.tools.registry import ToolDef, ToolRegistry


IDENTITY = {"agent_id": "a", "team_id": "t", "workspace_root": "/workspace", "worker_type": "worker"}


def test_unknown_write_uses_original_risk_even_if_registry_now_read():
    registry = ToolRegistry()
    registry.register(ToolDef("write_file", "read", {}, lambda: "ok", risk="read"))
    messages = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
    ]}]
    restored, state = restore_conversation({"messages": messages, "state": {
        "identity": IDENTITY, "turns_used": 3, "team_message_cursor": 2,
        "pending_calls": {"x": {"name": "write_file", "risk": "write"}},
    }}, IDENTITY)
    assert state["write_blocked"] is True
    assert state["turns_used"] == 3
    assert restored[-1]["tool_call_id"] == "x"
    assert "outcome_unknown" in restored[-1]["content"]
    assert "未执行" not in restored[-1]["content"]
    assert len(messages) == 2


def test_bad_identity_and_orphan_result_fail_closed():
    with pytest.raises(ConversationCorrupt):
        restore_conversation({"messages": [{"role": "user", "content": "task"}], "state": {
            "identity": {**IDENTITY, "agent_id": "wrong"}, "turns_used": 1,
        }}, IDENTITY)
    with pytest.raises(ConversationCorrupt):
        restore_conversation({"messages": [{"role": "tool", "tool_call_id": "orphan", "content": "ok"}], "state": {
            "identity": IDENTITY, "turns_used": 1,
        }}, IDENTITY)


def test_persisted_copy_redacts_request_local_arguments_and_live_output():
    registry = ToolRegistry()
    registry.register(ToolDef("secret_read", "read", {}, lambda: "ok", argument_persistence="request_local"))
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "secret_read", "arguments": '{"token":"SECRET"}'}}
    ]}, {"role": "tool", "tool_call_id": "x", "content": "placeholder", "api_content": "SECRET OUTPUT"}]
    saved = persistent_messages(messages, registry)
    assert "SECRET" not in json.dumps(saved)
    assert "SECRET" in json.dumps(messages)


@pytest.mark.parametrize("bad_state", [
    {"pending_calls": {"lost": {"name": "write_file", "risk": "write"}}},
    {"evidence_fragments": "not a list"},
    {"execution_observations": ["not an observation"]},
    {"turns_used": True},
    {"latest_assignment": {}},
])
def test_inconsistent_metadata_cannot_silently_reset_recovery(bad_state):
    with pytest.raises(ConversationCorrupt):
        restore_conversation({"messages": [{"role": "user", "content": "task"}], "state": {
            "identity": IDENTITY, "turns_used": 2, **bad_state,
        }}, IDENTITY)


def test_team_approval_classification_does_not_authorize_recovery_writes():
    registry = ToolRegistry()
    for name in ("team_task", "team_send", "team_inbox"):
        registry.register(ToolDef(name, "coordination", {}, lambda: "ok", risk="read"))
    assert recovery_tool_risk(registry, "team_task", '{"action":"list"}') == "read"
    assert recovery_tool_risk(registry, "team_task", '{"action":"create"}') == "write"
    assert recovery_tool_risk(registry, "team_send", "{}") == "write"
    assert recovery_tool_risk(registry, "team_inbox", '{"acknowledge":false}') == "write"
