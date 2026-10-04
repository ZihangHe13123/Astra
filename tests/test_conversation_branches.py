"""Durable reply versions preserve each selected path and never replay tools."""
import copy
import json
import uuid

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.conversation_branches import BranchConflict, BranchPublicationError, ConversationBranches
from agent.runtime.message_source import message_source_ref
from agent.runtime.session_store import SessionStore


def user(text):
    return {"role": "user", "content": text}


def answer(text):
    return {"role": "assistant", "content": text}


def initial(tmp_path, messages=None):
    path = tmp_path / "conversation.json"
    SessionStore(path).save({"messages": messages or [user("one"), answer("original")], "system_prompt": "system"})
    return path, ConversationBranches(path)


def prepare(branches, index):
    state = branches.state()
    messages = SessionStore(branches.logical_path).load(readonly=True)["messages"]
    return branches.prepare_retry(message_source_ref(messages[index], index),
                                  branch_id=state["branch_id"], expected_revision=state["revision"])


def complete(branches, prepared, text="alternative"):
    data = copy.deepcopy(prepared["seed"])
    data["messages"].append(answer(text))
    SessionStore(branches.logical_path, branch_id=prepared["candidate_branch_id"]).save(data)
    return branches.finish_candidate(prepared["candidate_branch_id"], expected_revision=prepared["revision"])


def select(branches, group, version):
    state = branches.state()
    return branches.select_version(group, version, branch_id=state["branch_id"], expected_revision=state["revision"])


def append(branches, *messages):
    store = SessionStore(branches.logical_path)
    data = store.load(readonly=True)
    count = len(data["messages"])
    data["messages"].extend(messages)
    store.save(data, append_from=count)


@pytest.mark.parametrize("provenance", ["peer_message", "session_wakeup", "goal_continuation", "command_workflow"])
def test_automatic_turn_cannot_replace_a_previous_human_reply_target(tmp_path, provenance):
    messages = [user("human request"), answer("human answer"),
                {**user("automatic follow-up"), "provenance": provenance}, answer("automatic answer")]
    _, branches = initial(tmp_path, messages)
    state = branches.state()
    assert [target["source_ref"]["index"] for target in state["targets"]] == [1]
    with pytest.raises(BranchConflict, match="complete final reply"):
        prepare(branches, 3)
    candidate = prepare(branches, 1)
    assert candidate["seed"]["messages"] == [messages[0]]


def test_legacy_read_is_side_effect_free_and_has_proven_targets(tmp_path):
    path = tmp_path / "legacy.json"
    messages = [user("question"), answer("thinking"), answer("final"),
                {"role": "user", "content": "delegate result", "provenance": "delegate"}]
    path.write_text(json.dumps({"messages": messages}))
    before = list(tmp_path.rglob("*"))
    store = SessionStore(path)
    state = ConversationBranches(path).state()
    assert store.branch_id == "main" and store.legacy_path == path and store.logical_path == path
    assert store.session_scope == "legacy"
    assert state == {"revision": 0, "branch_id": "main", "active_branch": "main", "choices": [],
                     "groups": [], "targets": [{"source_ref": message_source_ref(messages[2], 2)}]}
    assert list(tmp_path.rglob("*")) == before


def test_prepare_keeps_completed_tools_and_old_head_unchanged(tmp_path):
    messages = [user("do it"), {"role": "assistant", "content": "", "tool_calls": [
        {"id": "write-1", "type": "function", "function": {"name": "write", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "write-1", "content": "done"}, answer("original"),
        user("later"), answer("later answer")]
    path, branches = initial(tmp_path, messages)
    old = {p: p.read_bytes() for p in SessionStore(path).related_paths() if p.is_file()}
    prepared = prepare(branches, 3)
    assert prepared["seed"]["messages"] == messages[:3]
    assert prepared["seed"]["runtime_context_projection"] == {}
    assert branches.state()["active_branch"] == "main"
    assert all(p.read_bytes() == content for p, content in old.items())
    checkpoint = branches.checkpoint(branches.state()["groups"][0]["selected_version"])
    assert checkpoint["messages"] == messages[:4]
    state = complete(branches, prepared)
    assert state["active_branch"] == prepared["candidate_branch_id"]
    assert SessionStore(path).load(readonly=True)["messages"] == messages[:3] + [answer("alternative")]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_candidate_remains_visible_without_selecting(tmp_path, status):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    state = branches.finish_candidate(prepared["candidate_branch_id"], status=status)
    assert state["active_branch"] == "main"
    assert state["groups"][0]["versions"][1]["status"] == status
    assert SessionStore(path, branch_id=prepared["candidate_branch_id"]).exists
    with pytest.raises(BranchConflict, match="not complete"):
        select(branches, prepared["group_id"], prepared["version_id"])
    retry = prepare(branches, 1)
    assert retry["group_id"] == prepared["group_id"]
    assert len(branches.state()["groups"][0]["versions"]) == 3


def test_nested_roundtrip_recovers_selected_continuations_after_restart(tmp_path):
    path, branches = initial(tmp_path, [user("one"), answer("a1"), user("two"), answer("a2")])
    outer = prepare(branches, 1)
    original_outer = branches.state()["groups"][0]["selected_version"]
    complete(branches, outer, "b1")
    append(branches, user("new two"), answer("b2"))
    inner = prepare(branches, 3)
    original_inner = branches.state()["groups"][1]["selected_version"]
    complete(branches, inner, "c2")
    append(branches, user("three"), answer("c3"))
    branches = ConversationBranches(path)
    select(branches, outer["group_id"], original_outer)
    assert SessionStore(path).load(readonly=True)["messages"][-1] == answer("a2")
    selected = select(branches, outer["group_id"], outer["version_id"])
    assert selected["branch_id"] == inner["candidate_branch_id"]
    assert SessionStore(path).load(readonly=True)["messages"][-1] == answer("c3")
    select(branches, inner["group_id"], original_inner)
    append(branches, user("other three"), answer("b3"))
    select(branches, outer["group_id"], original_outer)
    select(branches, outer["group_id"], outer["version_id"])
    assert branches.state()["branch_id"] == outer["candidate_branch_id"]
    assert SessionStore(path).load(readonly=True)["messages"][-1] == answer("b3")


def test_creating_outer_group_after_inner_group_propagates_to_sibling_heads(tmp_path):
    _, branches = initial(tmp_path, [user("one"), answer("a1"), user("two"), answer("a2")])
    inner = prepare(branches, 3)
    original_inner = branches.state()["groups"][0]["selected_version"]
    complete(branches, inner, "b2")
    outer = prepare(branches, 1)
    original_outer = branches.state()["groups"][0]["selected_version"]
    complete(branches, outer, "b1")
    select(branches, outer["group_id"], original_outer)
    assert branches.state()["branch_id"] == inner["candidate_branch_id"]
    select(branches, inner["group_id"], original_inner)
    assert [group["id"] for group in branches.state()["groups"]] == [outer["group_id"], inner["group_id"]]
    select(branches, outer["group_id"], outer["version_id"])
    select(branches, outer["group_id"], original_outer)
    assert branches.state()["branch_id"] == "main"


def test_pinned_store_context_media_and_stage_do_not_follow_active_selection(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    pinned = SessionStore(path)
    context = AgentContext()
    context.set_session(str(path))
    assert context.load()
    prepared = prepare(branches, 1)
    complete(branches, prepared)
    assert context.branch_id == pinned.branch_id == "main"
    assert pinned.legacy_path == path
    assert SessionStore(path).branch_id == prepared["candidate_branch_id"]
    monkeypatch.setattr(SessionStore, "recover_interrupted", lambda *args: pytest.fail("preview must be read-only"))
    staged = context.stage_session(str(path), branch_id=prepared["candidate_branch_id"])
    assert staged.session_path == context.session_path == str(path)
    assert staged.session_scope != context.session_scope
    assert staged._session_store.appshot_media.path == pinned.appshot_media.path
    assert staged.messages[-1] == answer("alternative")
    assert context.messages[-1] == answer("original")
    assert SessionStore.for_subagent(path, "worker", branch_id="main").legacy_path.parent == tmp_path / ".artifacts"
    assert SessionStore.for_subagent(path, "worker", branch_id=staged.branch_id).legacy_path.parent == staged._session_store.legacy_path.parent / ".artifacts"


def test_compaction_preserves_immutable_versions_and_metadata_query_never_loads(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    complete(branches, prepared)
    frozen = branches.checkpoint(prepared["version_id"])
    store = SessionStore(path)
    store.save({"messages": [user("compressed summary"), answer("later")]}, append_from=0, snapshot=True)
    state = branches.state()
    assert state["groups"][0]["response_text"] == "alternative"
    assert "source_ref" not in state["groups"][0]
    assert branches.checkpoint(prepared["version_id"]) == frozen
    monkeypatch.setattr(SessionStore, "load", lambda *args, **kwargs: pytest.fail("warm query loaded history"))
    assert branches.state(messages=[])["groups"] == state["groups"]


def test_exact_source_stale_revision_and_incomplete_tool_chain_are_rejected(tmp_path):
    path, branches = initial(tmp_path, [user("one"), answer("progress"), answer("final")])
    with pytest.raises(BranchConflict, match="final reply"):
        prepare(branches, 1)
    before = branches.state()
    prepared = prepare(branches, 2)
    with pytest.raises(BranchConflict, match="changed"):
        branches.prepare_retry(before["targets"][0]["source_ref"], branch_id="main", expected_revision=0)
    branches.finish_candidate(prepared["candidate_branch_id"], status="cancelled")
    append(branches, user("pending"), {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}, answer("unsafe"))
    with pytest.raises(BranchConflict, match="complete final"):
        prepare(branches, 5)
    assert not any(target["source_ref"]["index"] == 5 for target in branches.state()["targets"])
    assert SessionStore(path).branch_id == "main"


def test_identical_answers_at_different_turns_have_distinct_groups(tmp_path):
    _, branches = initial(tmp_path, [user("same"), answer("same"), user("same"), answer("same")])
    earlier = prepare(branches, 1)
    branches.finish_candidate(earlier["candidate_branch_id"], status="cancelled")
    later = prepare(branches, 3)
    assert later["group_id"] != earlier["group_id"]
    assert [group["source_ref"]["index"] for group in branches.state()["groups"]] == [1, 3]


def test_publish_failure_leaves_old_active_and_unregistered_files_ignored(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    real_write = branches._atomic_json

    def fail_manifest(destination, value):
        if destination == branches.manifest_path:
            raise OSError("simulated disk full")
        real_write(destination, value)

    monkeypatch.setattr(branches, "_atomic_json", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        prepare(branches, 1)
    assert branches.state()["revision"] == 0
    assert SessionStore(path).branch_id == "main"
    orphan = next((branches.root / "heads").glob("*.json"))
    with pytest.raises(ValueError, match="Unknown"):
        SessionStore(path, branch_id=orphan.stem)
    assert orphan in branches.all_related_paths()


def test_finish_requires_persisted_exact_seed_and_failed_publication_can_retry(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    with pytest.raises(BranchConflict, match="saved completely"):
        branches.finish_candidate(prepared["candidate_branch_id"])
    data = copy.deepcopy(prepared["seed"])
    data["messages"] += [user("unrelated"), answer("bad")]
    SessionStore(path, branch_id=prepared["candidate_branch_id"]).save(data)
    with pytest.raises(BranchConflict, match="saved completely"):
        branches.finish_candidate(prepared["candidate_branch_id"])
    real_write = branches._atomic_json

    def fail_manifest(destination, value):
        if destination == branches.manifest_path:
            raise OSError("simulated commit failure")
        real_write(destination, value)

    monkeypatch.setattr(branches, "_atomic_json", fail_manifest)
    with pytest.raises(OSError, match="commit failure"):
        complete(branches, prepared)
    assert branches.state()["active_branch"] == "main"
    monkeypatch.setattr(branches, "_atomic_json", real_write)
    assert branches.finish_candidate(prepared["candidate_branch_id"])["active_branch"] == prepared["candidate_branch_id"]


def test_only_explicit_writer_recovery_marks_dead_candidate(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    monkeypatch.setattr(SessionStore, "_process_alive", lambda pid: False)
    before = branches.manifest_path.read_bytes()
    assert branches.state()["groups"][0]["versions"][1]["status"] == "generating"
    assert branches.manifest_path.read_bytes() == before
    assert branches.recover_interrupted()
    assert not branches.recover_interrupted()
    assert branches.state()["active_branch"] == "main"
    assert branches.state()["groups"][0]["versions"][1]["status"] == "interrupted"
    assert SessionStore(path, branch_id=prepared["candidate_branch_id"]).exists


def test_related_paths_contains_all_branches_and_rejects_symlink_before_delete(tmp_path):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    complete(branches, prepared)
    child = SessionStore.for_subagent(path, "worker", branch_id="main")
    child.save({"messages": [user("task"), answer("done")]})
    child.append_diagnostic({"error": "worker diagnostic"})
    paths = SessionStore(path).related_paths()
    assert path.with_suffix(".jsonl") in paths and branches.manifest_path in paths
    assert child.jsonl_path in paths
    assert child.diagnostic_path in paths
    assert branches._checkpoint_path(prepared["version_id"]) in paths
    for unsafe in ["../escape", "Main", "a" * 31, "x" * 32]:
        with pytest.raises(ValueError):
            SessionStore(path, branch_id=unsafe)
    with pytest.raises(ValueError):
        SessionStore(path, branch_id=uuid.uuid4().hex)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = branches.root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError, match="owned path"):
        SessionStore(path).related_paths()
    assert outside.is_dir()


def test_manifest_cannot_redirect_completed_version_to_unrelated_head(tmp_path):
    _, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    complete(branches, prepared)
    manifest = json.loads(branches.manifest_path.read_text())
    manifest["groups"][prepared["group_id"]]["versions"][prepared["version_id"]]["last_head"] = "main"
    branches.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OSError, match="invalid"):
        branches.state(messages=[])


def test_post_replace_fsync_failure_recognizes_published_prepare_and_completion(tmp_path, monkeypatch):
    path, branches = initial(tmp_path)
    real_sync = branches._sync_directory

    def fail_after_manifest_replace(directory):
        if directory == branches.root:
            raise OSError("directory fsync failed after replace")
        real_sync(directory)

    monkeypatch.setattr(branches, "_sync_directory", fail_after_manifest_replace)
    prepared = prepare(branches, 1)
    assert branches.state()["revision"] == prepared["revision"]
    assert branches.state()["active_branch"] == "main"
    state = complete(branches, prepared)
    assert state["active_branch"] == prepared["candidate_branch_id"]
    assert SessionStore(path).load(readonly=True)["messages"][-1] == answer("alternative")


def test_unreadable_post_publication_error_preserves_candidate_identity(tmp_path, monkeypatch):
    _, branches = initial(tmp_path)
    reference = branches.state()["targets"][0]["source_ref"]
    real_write, real_read = branches._atomic_json, branches._read
    published = False

    def fail_after_replace(destination, value):
        nonlocal published
        real_write(destination, value)
        if destination == branches.manifest_path:
            published = True
            raise OSError("post-replace failure")

    def unavailable_after_replace():
        if published:
            raise OSError("temporary read failure")
        return real_read()

    monkeypatch.setattr(branches, "_atomic_json", fail_after_replace)
    monkeypatch.setattr(branches, "_read", unavailable_after_replace)
    with pytest.raises(BranchPublicationError) as caught:
        branches.prepare_retry(reference, branch_id="main", expected_revision=0)
    assert caught.value.publication_unknown
    manifest = real_read()
    assert caught.value.candidate_branch_id in manifest["branches"]
    assert manifest["active_branch"] == "main"


def test_dated_canonical_turn_retains_group_identity_after_prefix_compaction(tmp_path):
    messages = [user("earlier"), answer("old answer"),
                {**user("target"), "timestamp": 123.0}, {**answer("original"), "timestamp": 124.0}]
    path, branches = initial(tmp_path, messages)
    prepared = prepare(branches, 3)
    data = copy.deepcopy(prepared["seed"])
    response = {**answer("alternative"), "timestamp": 125.0}
    data["messages"].append(response)
    SessionStore(path, branch_id=prepared["candidate_branch_id"]).save(data)
    branches.finish_candidate(prepared["candidate_branch_id"])
    compacted = [user("summary"), messages[2], response]
    SessionStore(path).save({"messages": compacted}, snapshot=True)
    state = branches.state()
    assert state["groups"][0]["source_ref"] == message_source_ref(response, 2)
    retried = prepare(branches, 2)
    assert retried["group_id"] == prepared["group_id"]
    assert len(branches.state()["groups"]) == 1


def test_branch_storage_rejects_artifact_directory_symlink(tmp_path):
    path, branches = initial(tmp_path)
    prepared = prepare(branches, 1)
    physical = SessionStore(path, branch_id=prepared["candidate_branch_id"]).legacy_path
    outside = tmp_path / "unrelated"
    outside.mkdir()
    try:
        (physical.parent / ".artifacts").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError, match="owned path"):
        SessionStore(path, branch_id=prepared["candidate_branch_id"])
    assert not list(outside.iterdir())


def test_legacy_synthetic_user_envelopes_are_not_regeneration_anchors(tmp_path):
    messages = [{**user("actual"), "provenance": "user"}, answer("first"),
                user("[SYSTEM-DELIVERED SUBAGENT RESULT worker] done"), answer("final"),
                {**user("compaction recap"), "metadata": {"synthetic": True}}]
    _, branches = initial(tmp_path, messages)
    assert branches.state()["targets"] == [{"source_ref": message_source_ref(messages[3], 3)}]
    prepared = prepare(branches, 3)
    assert branches.state()["groups"][0]["user_text"] == "actual"
    assert prepared["seed"]["messages"] == messages[:3]
