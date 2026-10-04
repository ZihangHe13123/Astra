"""Presentation history must not alter canonical messages or cache prefixes."""
from __future__ import annotations

import copy
import json

import pytest

from agent.cli import sessions
from agent.runtime.context import AgentContext
from agent.runtime.message_source import message_source_ref
from agent.runtime.session_store import SessionStore
from agent.ui import history_index, queries
from agent.ui.reasoning import legacy_reasoning_text, reasoning_display_fields


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(sessions, "SESSION_DIR", root)
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    return SessionStore(root / "reasoning.json")


def originals(store):
    return {path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in store.related_paths() if path.is_file()}


def test_saved_reasoning_is_paged_on_the_original_assistant_without_mutating_source_or_prompt(store):
    context = AgentContext(system_prompt="Stable cache prefix")
    context.set_session(str(store.logical_path))
    context.add_user("question")
    context.add_assistant_raw({"role": "assistant", "content": "", "reasoning_content": "tool reasoning\n",
        "tool_calls": [{"id": "r", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        "_provider_state": {"encrypted_content": "opaque; never rendered"}})
    context.add_tool("r", "Completed observation")
    context.add_assistant_raw({"role": "assistant", "content": "answer", "reasoning_content": "  final reasoning\n"})
    context.save()
    before = originals(store)
    canonical = copy.deepcopy(context.messages)
    prompt = json.dumps(context.get_prompt(), ensure_ascii=False, sort_keys=True)
    tokens = context.estimate_prompt_tokens()
    token_costs, saved_cursor = list(context._message_token_costs), context._saved_message_count
    for enabled in (True, False, True):
        context.show_reasoning = enabled
        page = queries.history("reasoning", limit=1)
        assert page["total"] == 4 and page["before"] == 3
        assert page["messages"] == [{"id": "work:reasoning:3", "role": "assistant", "content": "answer",
            "timestamp": canonical[3]["timestamp"], "reasoning_content": "  final reasoning\n",
            "source_ref": message_source_ref(canonical[3], 3)}]
        earlier = queries.history("reasoning", before=3, limit=3)["messages"]
        assert earlier[-1]["reasoning_content"] == "tool reasoning\n"
        assert "_provider_state" not in earlier[-1]
        assert context.messages == canonical
        assert json.dumps(context.get_prompt(), ensure_ascii=False, sort_keys=True) == prompt
        assert context.estimate_prompt_tokens() == tokens
        assert context._message_token_costs == token_costs
        assert context._saved_message_count == saved_cursor
    assert originals(store) == before
    assert context.get_prompt()[2]["reasoning_content"] == "tool reasoning\n"


def test_legacy_reasoning_stays_at_its_own_source_position_without_migration(store):
    messages = [{"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "r", "content": "observation"},
        {"role": "user", "content": "[retired reasoning header]\n\nSaved old reasoning",
         "_meta": {"type": "reasoning_context"}},
        {"role": "assistant", "content": "answer"}]
    store.save({"messages": messages})
    before = originals(store)
    page = queries.history("reasoning", before=4, limit=1)
    assert page["total"] == 5 and page["before"] == 3
    assert page["messages"] == [{"id": "work:reasoning:3", "role": "reasoning", "content": "Saved old reasoning",
        "timestamp": None, "source_ref": message_source_ref(messages[3], 3)}]
    assert queries.history("reasoning", source_ref=message_source_ref(messages[3], 3))["target_id"] == "work:reasoning:3"
    assert store.load(readonly=True)["messages"] == messages
    assert originals(store) == before


def test_silent_wakeup_reasoning_stays_hidden(store):
    store.save({"messages": [{"role": "user", "content": "background", "provenance": "session_wakeup"},
        {"role": "assistant", "content": "quiet", "reasoning_content": "background reasoning"},
        {"role": "user", "content": "visible"},
        {"role": "assistant", "content": "answer", "reasoning_content": "visible reasoning"}]})
    page = queries.history("reasoning")
    assert len(page["messages"]) == 2
    assert page["messages"][-1]["reasoning_content"] == "visible reasoning"
    assert "background reasoning" not in json.dumps(page)


def test_reasoning_projection_follows_the_selected_reply_branch(store):
    from agent.runtime.conversation_branches import ConversationBranches
    messages = [{"role": "user", "content": "question"},
                {"role": "assistant", "content": "A", "reasoning_content": "thought A"}]
    store.save({"messages": messages})
    branches = ConversationBranches(store.logical_path)
    prepared = branches.prepare_retry(message_source_ref(messages[1], 1), branch_id="main", expected_revision=0)
    seed = copy.deepcopy(prepared["seed"])
    seed["messages"].append({"role": "assistant", "content": "B", "reasoning_content": "thought B"})
    SessionStore(store.logical_path, branch_id=prepared["candidate_branch_id"]).save(seed)
    state = branches.finish_candidate(prepared["candidate_branch_id"])
    assert queries.history("reasoning")["messages"][-1]["reasoning_content"] == "thought B"
    group = state["groups"][0]
    branches.select_version(group["id"], group["versions"][0]["id"],
                            branch_id=state["branch_id"], expected_revision=state["revision"])
    assert queries.history("reasoning")["messages"][-1]["reasoning_content"] == "thought A"
    assert SessionStore(store.logical_path, branch_id="main").load(readonly=True)["messages"] == messages


def test_legacy_reasoning_is_not_a_human_boundary_that_reveals_silent_wakeup(store):
    from agent.runtime.session_wakeup import visible_wakeup_history
    messages = [{"role": "user", "content": "background", "provenance": "session_wakeup"},
        {"role": "assistant", "content": "silent tool call"},
        {"role": "tool", "content": "silent observation"},
        {"role": "user", "content": "[header]\n\nSILENT_BACKGROUND_REASONING", "_meta": {"type": "reasoning_context"}},
        {"role": "assistant", "content": "silent final answer"},
        {"role": "user", "content": "human"}, {"role": "assistant", "content": "visible answer"}]
    store.save({"messages": messages})
    assert visible_wakeup_history(messages) == messages[-2:]
    page = queries.history("reasoning")
    assert [message["content"] for message in page["messages"]] == ["human", "visible answer"]
    assert page["messages"][-1]["source_ref"] == message_source_ref(messages[-1], 6)
    assert "SILENT_BACKGROUND_REASONING" not in json.dumps(page)


@pytest.mark.parametrize("value", [None, "", " \n", 5, {"encrypted_content": "secret"}, ["text"]])
def test_only_saved_assistant_plaintext_is_displayed(value):
    assert reasoning_display_fields({"role": "assistant", "reasoning_content": value}) == {}
    assert legacy_reasoning_text({"role": "user", "content": value, "_meta": {"type": "reasoning_context"}}) is None


def test_projection_cache_upgrade_and_delete_include_the_previous_format(store):
    store.save({"messages": [{"role": "assistant", "content": "answer", "reasoning_content": "saved"}]})
    old = history_index._cache_path(store, version=2)
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old projection without reasoning")
    for version in (1, 2, 3):
        history_index._cache_path(store, version=version).touch(exist_ok=True)
    page = queries.history("reasoning")
    assert page["messages"][0]["reasoning_content"] == "saved"
    assert old.read_bytes() == b"old projection without reasoning"
    history_index.discard_history_index(store)
    assert all(not history_index._cache_path(store, version=version).exists() for version in (1, 2, 3))
