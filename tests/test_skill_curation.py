import asyncio
import json
from pathlib import Path

import pytest

from agent.cli.learning_commands import execute_learning_command
from agent.runtime.instance_lock import InstanceAlreadyRunning
from agent.runtime.learning import LearningStore
from agent.runtime.skill_curation import SkillCurator
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.skills import register_skill_tools
from tests.learning_fixtures import stage_legacy_proposal


def skill(name, body="Read the error, check its cause, and report limits."):
    return f"---\nname: {name}\ndescription: Debug a reusable failure\n---\n\n# Steps\n{body}\n"


class Model:
    def __init__(self, actions):
        self.actions = actions
        self.calls = []

    async def chat_limited(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": json.dumps({"actions": self.actions}), "finish_reason": "stop"}


def action(kind, *names, content=None):
    value = {"action": kind, "names": list(names), "reason": "A source-supported content review."}
    if content is not None:
        value["content"] = content
    return value


@pytest.fixture
def learned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEARNING_REVIEW_MODE", "review")
    return LearnedSkills(SkillStore(tmp_path / "skills"))


def save(store, name="debug-one", body=None):
    return store.manage("create", name, content=skill(name, body) if body else skill(name),
                        source={"session_id": "session-test", "excerpts": [{"role": "tool", "excerpt": "Real check returned exit 1."}]})


def test_direct_save_visible_and_undoable(learned):
    record = save(learned)
    assert record["status"] == "saved"
    assert "debug-one" in {s["name"] for s in learned.skills.list()}
    assert learned._path("debug-one").exists()
    assert learned.history()[0]["source"]["session_id"] == "session-test"
    learned.undo(record["run_id"])
    assert "debug-one" not in {s["name"] for s in learned.skills.list()}


def test_tool_writes_skill_instead_of_candidate(learned):
    registry = ToolRegistry()
    register_skill_tools(registry, learned.skills, session_id=lambda: "real-session")
    result = asyncio.run(registry.execute("skill_manage", {"action": "create", "origin": "auto", "name": "debug-one", "content": skill("debug-one")}))
    assert not result["error"]
    assert json.loads(result["output"])["status"] == "saved"
    assert LearningStore(learned.root.parent / "learning.db").count() == 0
    assert not {"learning", "learning_search"}.intersection(registry.tool_names)


def test_broken_journal_does_not_prevent_read_tool_registration(learned):
    learned.home.mkdir(parents=True, exist_ok=True)
    journal = learned.home / "journal.json"
    journal.write_text("{broken", encoding="utf-8")
    registry = ToolRegistry()
    register_skill_tools(registry, learned.skills)
    assert not asyncio.run(registry.execute("skills_list", {}))["error"]
    result = asyncio.run(registry.execute("skill_manage", {
        "action": "create", "origin": "auto", "name": "debug-one", "content": skill("debug-one"),
    }))
    assert result["error"]
    assert learned._snapshot("debug-one") == {}
    assert journal.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("file_path", ["references\\evidence.txt", "references/./evidence.txt"])
def test_supporting_paths_keep_canonical_ownership_and_undo(learned, file_path):
    save(learned)
    before = learned._snapshot("debug-one")
    result = learned.manage("write_file", "debug-one", file_path=file_path, content="Original evidence")
    owned = learned.owned("debug-one", learned._state())
    assert owned["references/evidence.txt"] == "Original evidence"
    learned.undo(result["run_id"])
    assert learned._snapshot("debug-one") == before


@pytest.mark.parametrize("protection", ["manual", "changed", "pinned", "scope", "symlink"])
def test_never_overwrites_protected_skills(learned, protection):
    if protection == "manual":
        learned.skills.create("debug-one", skill("debug-one"))
    else:
        save(learned)
        path = learned._path("debug-one")
        if protection == "changed":
            path.write_text(skill("debug-one", "User-added instruction"))
        elif protection == "pinned":
            path.write_text(path.read_text().replace("description:", "pinned: true\ndescription:"))
        elif protection == "scope":
            state = learned._state()
            state["skills"]["debug-one"]["scope"] = "another-workspace"
            learned._json(learned.home / "index.json", state)
        elif protection == "symlink":
            outside = learned.root.parent / "outside.md"
            outside.write_text(path.read_text())
            path.unlink()
            path.symlink_to(outside)
    with pytest.raises(ValueError):
        learned.manage("patch", "debug-one", old_string="Read the error", new_string="Delete everything")


def test_review_merges_preserves_sources_and_undo(learned):
    save(learned, "debug-one")
    save(learned, "debug-two")
    learned.manage("write_file", "debug-two", file_path="references/evidence.txt", content="A real supporting example")
    before = {name: learned._snapshot(name) for name in ("debug-one", "debug-two")}
    model = Model([action("merge", "debug-one", "debug-two", content=skill("debug-one", "Read references/evidence.txt. Check the failure and limits."))])
    record = asyncio.run(SkillCurator(model, learned).review())
    assert len(model.calls) == 1
    assert model.calls[0][1]["max_tokens"] == 4096
    assert model.calls[0][1]["disable_thinking"] is True
    assert model.calls[0][1]["max_retries"] == 0
    assert model.calls[0][1]["request_timeout"] == 90
    assert not learned._snapshot("debug-two")
    assert "references/evidence.txt" in learned._snapshot("debug-one")
    assert len(learned._state()["skills"]["debug-one"]["sources"]) >= 2
    learned.undo(record["id"])
    assert {name: learned._snapshot(name) for name in before} == before


def test_archive_is_removed_from_catalog_but_recoverable(learned):
    save(learned)
    record = asyncio.run(SkillCurator(Model([action("archive", "debug-one")]), learned).review())
    assert "debug-one" not in {x["name"] for x in learned.skills.list()}
    assert learned.history(record["id"])[0]["before"]["debug-one"]["SKILL.md"]
    learned.undo(record["id"])
    assert learned.skills.view("debug-one")


def test_empty_or_keep_review_still_has_record(learned):
    model = Model([])
    record = asyncio.run(SkillCurator(model, learned).review())
    assert not model.calls
    assert record["source"]["review"]["checked"] == 0
    save(learned)
    model.actions = [action("keep", "debug-one")]
    record = asyncio.run(SkillCurator(model, learned).review())
    assert record["source"]["review"]["counts"] == {"keep": 1}
    assert record["after"] == {}


@pytest.mark.parametrize("actions", [[], [action("keep", "unread")], [action("keep", "debug-one"), action("archive", "debug-one")],
                                     [action("rewrite", "debug-one", content="No frontmatter")], [action("merge", "debug-one")]])
def test_bad_plan_applies_nothing_and_does_not_advance_cursor(learned, actions):
    save(learned)
    before = learned._state()
    files = learned._snapshot("debug-one")
    with pytest.raises(ValueError):
        asyncio.run(SkillCurator(Model(actions), learned).review())
    assert learned._state() == before
    assert learned._snapshot("debug-one") == files


def test_user_edit_during_model_request_is_preserved(learned):
    save(learned)
    class EditingModel(Model):
        async def chat_limited(self, messages, **kwargs):
            learned._path("debug-one").write_text(skill("debug-one", "User edit during review"))
            return await super().chat_limited(messages, **kwargs)
    with pytest.raises(ValueError, match="edited outside"):
        asyncio.run(SkillCurator(EditingModel([action("archive", "debug-one")]), learned).review())
    assert "User edit" in learned.skills.view("debug-one")
    assert learned._state()["last_review"] is None


def test_edit_between_assessment_and_commit_is_preserved(learned, monkeypatch):
    save(learned)
    commit = learned.commit
    def raced(**kwargs):
        learned._path("debug-one").write_text(skill("debug-one", "Newer user change"))
        return commit(**kwargs)
    monkeypatch.setattr(learned, "commit", raced)
    with pytest.raises(ValueError, match="changed before commit"):
        asyncio.run(SkillCurator(Model([action("archive", "debug-one")]), learned).review())
    assert "Newer user change" in learned.skills.view("debug-one")
    assert learned._state()["last_review"] is None


def test_provider_rename_cannot_change_identity(learned):
    save(learned)
    with pytest.raises(ValueError, match="cannot rename"):
        asyncio.run(SkillCurator(Model([action("rewrite", "debug-one", content=skill("new-name"))]), learned).review())
    assert learned.skills.view("debug-one")
    assert not learned._snapshot("new-name")


@pytest.mark.parametrize("match", [True, False])
def test_compact_review_patch_requires_exact_input(learned, match):
    save(learned)
    patch = {**action("rewrite", "debug-one"), "patches": [{
        "old_string": "Read the error" if match else "text absent from this skill",
        "new_string": "Inspect the actual error",
    }]}
    if match:
        record = asyncio.run(SkillCurator(Model([patch]), learned).review())
        assert "Inspect the actual error" in learned.skills.view("debug-one")
        learned.undo(record["id"])
        assert "Read the error" in learned.skills.view("debug-one")
    else:
        with pytest.raises(ValueError, match="exactly once"):
            asyncio.run(SkillCurator(Model([patch]), learned).review())
        assert learned._state()["last_review"] is None


def test_history_state_symlink_is_not_followed(learned):
    save(learned)
    outside = learned.root.parent / "outside-index.json"
    outside.write_text("must not be read or overwritten")
    path = learned.home / "index.json"
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="symlinks"):
        learned._state()
    assert outside.read_text() == "must not be read or overwritten"


def test_bad_or_truncated_model_output_never_changes_files(learned):
    save(learned)
    class BadModel:
        async def chat_limited(self, *_args, **_kwargs):
            return {"content": '{"actions": []}', "finish_reason": "length"}
    with pytest.raises(ValueError, match="truncated"):
        asyncio.run(SkillCurator(BadModel(), learned).review())
    assert learned._state()["last_review"] is None


def test_retired_commands_do_not_activate_old_candidates(learned):
    store = LearningStore(learned.root.parent / "learning.db")
    item = stage_legacy_proposal(store, "legacy", "skill_create", {"name": "old-one", "content": skill("old-one")},
                                 "Historical candidate")
    for command in (["approve", "all"], ["repair", "all"], ["summarize"], ["legacy", "approve", item["id"]],
                    ["legacy", "pending"], ["pending"]):
        output, error = asyncio.run(execute_learning_command(store, learned.skills, command))
        assert "retired" in error and not output
    assert store.count() == 1
    assert not learned._snapshot("old-one")


def test_conflicting_undo_preserves_all_files(learned):
    save(learned, "debug-one")
    save(learned, "debug-two")
    record = asyncio.run(SkillCurator(Model([action("rewrite", "debug-one", content=skill("debug-one", "New method")), action("archive", "debug-two")]), learned).review())
    learned._path("debug-one").write_text(skill("debug-one", "A later user edit"))
    with pytest.raises(ValueError, match="Undo conflict"):
        learned.undo(record["id"])
    assert not learned._snapshot("debug-two")
    assert "later user edit" in learned.skills.view("debug-one")


def test_partial_multifile_failure_rolls_back(learned, monkeypatch):
    save(learned, "debug-one")
    save(learned, "debug-two")
    before = learned._state()
    real_replace = learned._replace
    calls = 0
    def flaky(name, files):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        real_replace(name, files)
    monkeypatch.setattr(learned, "_replace", flaky)
    with pytest.raises(OSError):
        asyncio.run(SkillCurator(Model([action("archive", "debug-one"), action("archive", "debug-two")]), learned).review())
    assert learned._state() == before
    assert learned.skills.view("debug-one") and learned.skills.view("debug-two")
    assert not (learned.home / "journal.json").exists()


def test_restart_recovers_interrupted_transaction(learned):
    save(learned)
    before = learned._state()
    record = learned.history()[0]
    record.update(id="sl_20260913T100000_1234abcd", kind="review", status="applying",
                  before={"debug-one": learned._snapshot("debug-one")}, after={"debug-one": {}},
                  state_before=before)
    learned._json(learned.home / "journal.json", record)
    learned._replace("debug-one", {})
    restarted = LearnedSkills(learned.skills)
    with restarted.locked():
        assert restarted.skills.view("debug-one")
    assert restarted._state() == before


def test_review_cancellation_and_lock_release(learned):
    save(learned)
    async def scenario():
        entered = asyncio.Event()
        class WaitingModel:
            async def chat_limited(self, *_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(SkillCurator(WaitingModel(), learned).review())
        await entered.wait()
        with pytest.raises(InstanceAlreadyRunning):
            save(LearnedSkills(learned.skills), "blocked")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        save(LearnedSkills(learned.skills), "now-allowed")
    asyncio.run(scenario())
    assert learned._state()["last_review"] is None


def test_timeout_no_retry_or_changes(learned, monkeypatch):
    save(learned)
    monkeypatch.setattr("agent.runtime.skill_curation.REVIEW_TIMEOUT", 0.01)
    class SlowModel:
        calls = 0
        async def chat_limited(self, *_args, **_kwargs):
            self.calls += 1
            await asyncio.sleep(1)
    model = SlowModel()
    with pytest.raises(ValueError):
        asyncio.run(SkillCurator(model, learned).review())
    assert model.calls == 1
    assert learned._state()["last_review"] is None


def test_round_robin_and_oversized_skip(learned, monkeypatch):
    for name in ("a-big", "b-small", "c-small"):
        save(learned, name, "x" * 10000 if name == "a-big" else "Read error and report limits.")
    monkeypatch.setattr("agent.runtime.skill_curation.MAX_INPUT_CHARS", 1600)
    monkeypatch.setattr("agent.runtime.skill_curation.MAX_ITEMS", 1)
    record = asyncio.run(SkillCurator(Model([action("keep", "b-small")]), learned).review())
    assert record["source"]["review"]["remaining"] == 1
    assert record["source"]["review"]["skipped"][0]["name"] == "a-big"
    restarted = LearnedSkills(learned.skills)
    record = asyncio.run(SkillCurator(Model([action("keep", "c-small")]), restarted).review())
    assert record["source"]["review"]["remaining"] == 0


def test_default_batch_leaves_room_for_rewrites_and_resumes_after_restart(learned):
    names = [f"debug-{index}" for index in range(5)]
    for name in names:
        save(learned, name)
    record = asyncio.run(SkillCurator(Model([action("keep", name) for name in names[:4]]), learned).review())
    assert record["source"]["review"]["checked"] == 4
    assert record["source"]["review"]["remaining"] == 1
    restarted = LearnedSkills(learned.skills)
    record = asyncio.run(SkillCurator(Model([action("keep", names[4])]), restarted).review())
    assert record["source"]["review"]["checked"] == 1
    assert record["source"]["review"]["remaining"] == 0


def test_off_disables_saving_and_direct_dispatch_cannot_bypass_conversational_review(learned):
    save(learned)
    store = LearningStore(learned.root.parent / "learning.db")
    store.set_mode("off")
    registry = ToolRegistry()
    register_skill_tools(registry, learned.skills)
    result = asyncio.run(registry.execute("skill_manage", {"action": "create", "origin": "auto", "name": "blocked", "content": skill("blocked")}))
    assert "Learning is off" in result["error"]
    output, error = asyncio.run(execute_learning_command(store, learned.skills, ["review"]))
    assert not output and "Review now starts a conversation" in error


def test_background_and_candidate_hooks_removed_from_runtime():
    root = Path(__file__).resolve().parents[1]
    for relative in ("agent/cli/backend.py", "agent/cli/main.py"):
        source = (root / relative).read_text()
        assert "advance_review_counter(" not in source
        assert "LEARNING_REVIEW_AUTO" not in source
        assert "register_learning_tools(" not in source
        assert "reset_review_counter(" not in source
    for retired in ("agent/runtime/tools/learning.py", "agent/runtime/learning_lifecycle.py",
                    "agent/runtime/learning_queue.py", "agent/cli/legacy_learning_commands.py"):
        assert not (root / retired).exists(), retired
    source = (root / "agent/runtime/react.py").read_text()
    assert "learning_reminder(" not in source
    assert "lifecycle.end_request(" not in source
