import asyncio
import sqlite3

import pytest

from agent.cli.memory_commands import execute_memory_command
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.tools.memory import register_memory_tools
from agent.runtime.tools.registry import ToolRegistry


def _assert_connection_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_memory_connections_close_after_context(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")

    with store._connection() as core_db:
        assert core_db.execute("SELECT 1").fetchone()[0] == 1
        assert core_db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with store.record_store._connection() as record_db:
        assert record_db.execute("SELECT 1").fetchone()[0] == 1
        assert record_db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    _assert_connection_closed(core_db)
    _assert_connection_closed(record_db)


def write_legacy_core(core_dir, *, memory=(), user=()):
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "MEMORY.md").write_text("\n§\n".join(memory) + ("\n" if memory else ""), encoding="utf-8")
    (core_dir / "USER.md").write_text("\n§\n".join(user) + ("\n" if user else ""), encoding="utf-8")


def test_core_memory_persists_in_human_readable_markdown(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    store = MemoryStore(path, core_dir=core_dir)
    memory = store.add_core("memory", "Search uses Exa and SearXNG")
    user = store.add_core("user", "User prefers concise Chinese answers")

    reloaded = MemoryStore(path, core_dir=core_dir)
    assert reloaded.list_core("memory") == [memory]
    assert reloaded.list_core("user") == [user]
    assert (core_dir / "MEMORY.md").read_text(encoding="utf-8").strip() == memory["content"]
    assert (core_dir / "USER.md").read_text(encoding="utf-8").strip() == user["content"]


def test_core_usage_reports_capacity_for_one_more_entry(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    store.memory_char_limit = 20

    assert store.core_usage("memory")["available_chars"] == 20
    store.add_core("memory", "1234567890")
    assert store.core_usage("memory")["available_chars"] == 7


def test_markdown_core_is_the_direct_source_of_truth(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    memory_entries = ("First durable agent decision", "Second environment fact " + "x" * 700)
    user_entries = ("User prefers Chinese", "Test user is preparing for Northstar University")
    write_legacy_core(core_dir, memory=memory_entries, user=user_entries)
    before = {name: (core_dir / name).read_bytes() for name in ("MEMORY.md", "USER.md")}

    store = MemoryStore(path, core_dir=core_dir)

    assert [item["content"] for item in store.list_core("memory")] == list(memory_entries)
    assert [item["content"] for item in store.list_core("user")] == list(user_entries)
    assert {name: (core_dir / name).read_bytes() for name in before} == before
    assert store.record_store.list_records() == []
    assert list(core_dir.glob("_backup_core_migration_*")) == []
    for scope, entries in (("memory", memory_entries), ("user", user_entries)):
        for entry in entries:
            assert entry in store.format_core_prompt()


def test_markdown_core_ids_are_stable_across_restarts(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    write_legacy_core(core_dir, memory=("Keep this once",), user=("Answer in Chinese",))

    first = MemoryStore(path, core_dir=core_dir)
    first_ids = [item["record_id"] for scope in ("memory", "user") for item in first.list_core(scope)]
    second = MemoryStore(path, core_dir=core_dir)
    second_ids = [item["record_id"] for scope in ("memory", "user") for item in second.list_core(scope)]

    assert second_ids == first_ids
    assert second.record_store.list_records() == []


def test_oversized_manual_markdown_is_bounded_at_prompt_render(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    write_legacy_core(core_dir, memory=("x" * 1500, "y" * 1000))
    store = MemoryStore(path, core_dir=core_dir)

    usage = store.core_usage("memory")
    assert usage["over_limit"] is True
    assert usage["injected_chars"] <= 2200
    assert "x" * 1500 in store.format_core_prompt()
    assert "y" * 1000 not in store.format_core_prompt()
    with pytest.raises(ValueError, match="hard limit"):
        store.import_core_markdown()


def test_external_markdown_edits_are_immediately_visible_when_valid(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    write_legacy_core(core_dir, memory=("Existing decision",))
    store = MemoryStore(path, core_dir=core_dir)

    (core_dir / "MEMORY.md").write_text(
        "Existing decision\n§\nExternally added decision\n",
        encoding="utf-8",
    )
    assert [item["content"] for item in store.list_core("memory")] == [
        "Existing decision",
        "Externally added decision",
    ]

    result = store.import_core_markdown()

    assert result == {"imported": 0, "total": 2}
    assert list(core_dir.glob("_backup_core_migration_*")) == []


def test_memory_validate_core_slash_command_checks_direct_markdown(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    write_legacy_core(core_dir, user=("Original user fact",))
    store = MemoryStore(path, core_dir=core_dir)
    (core_dir / "USER.md").write_text(
        "Original user fact\n§\nImported user preference\n",
        encoding="utf-8",
    )

    output, error = execute_memory_command(store, ["validate-core"], session_id="s1")

    assert error == ""
    assert "Validated Core Markdown hard limits" in output
    assert [item["content"] for item in store.list_core("user")] == [
        "Original user fact",
        "Imported user preference",
    ]


def test_direct_markdown_core_remove_updates_file(tmp_path):
    path = tmp_path / "memory.db"
    core_dir = tmp_path / "core"
    store = MemoryStore(path, core_dir=core_dir)
    item = store.add_core("user", "User prefers detailed answers")

    assert store.remove_core(item["id"]) is True
    assert store.list_core("user") == []
    assert (core_dir / "USER.md").read_text(encoding="utf-8") == ""


def test_markdown_core_is_not_copied_into_legacy_structured_recall(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    core = store.add_core("user", "User prefers concise answers")
    regular = store.add_record(
        kind="user_fact",
        content="User is preparing an application",
    )

    normal = store.recall_records("User", limit=10)
    assert [item.record_id for item in normal] == [regular.record_id]
    assert core["record_id"] not in {item.record_id for item in normal}


def test_core_memory_deduplicates_enforces_capacity_and_rejects_secrets(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    first = store.add_core("memory", "Use the local endpoint")
    assert store.add_core("memory", "Use the local endpoint")["id"] == first["id"]

    with pytest.raises(ValueError, match="secret or credential"):
        store.add_core("memory", "API_KEY=super-secret-value")

    store.add_core("memory", "x" * 600)
    store.add_core("memory", "y" * 600)
    store.add_core("memory", "z" * 600)
    with pytest.raises(ValueError, match="MEMORY.md is full"):
        store.add_core("memory", "q" * 400)


def test_legacy_sqlite_core_is_migrated_without_project_scoping(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE core_memories (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, content TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO core_memories(scope, content) VALUES (?, ?)",
            [
                ("user", "Answer in Chinese"),
                ("project:c:/old-project", "Use Exa search"),
            ],
        )

    store = MemoryStore(path)
    assert [item["content"] for item in store.list_core("user")] == ["Answer in Chinese"]
    assert [item["content"] for item in store.list_core("memory")] == ["Use Exa search"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM core_memories").fetchone()[0] == 0


def test_working_memory_is_session_isolated_and_survives_restart(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.update_working("session-a", "goal", "Implement memory")
    store.update_working("session-a", "artifacts", "agent/runtime/memory.py")
    store.update_working("session-b", "goal", "Unrelated task")

    reloaded = MemoryStore(path)
    assert reloaded.get_working("session-a") == {
        "artifacts": "agent/runtime/memory.py",
        "goal": "Implement memory",
    }
    assert reloaded.get_working("session-b") == {"goal": "Unrelated task"}

    assert reloaded.rename_working("session-b", "renamed") is True
    assert reloaded.get_working("session-b") == {}
    assert reloaded.get_working("renamed") == {"goal": "Unrelated task"}
    assert reloaded.clear_working("session-a") is True
    assert reloaded.get_working("session-a") == {}
    assert reloaded.get_working("renamed") == {"goal": "Unrelated task"}


def test_structured_working_plan_tracks_steps_and_advances_current_step(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    plan = store.set_working_plan(
        "session-a",
        ["Inspect current flow", "Implement progress events", "Run regression tests"],
        goal="Show task progress in the TUI",
    )
    assert plan["steps"] == [
        {"text": "Inspect current flow", "status": "in_progress"},
        {"text": "Implement progress events", "status": "pending"},
        {"text": "Run regression tests", "status": "pending"},
    ]

    updated = store.update_working_step("session-a", 1, "completed")
    assert updated["steps"][0]["status"] == "completed"
    assert updated["steps"][1]["status"] == "in_progress"
    # Legacy plan APIs remain compatible, but Case/TaskRun is now the prompt authority.
    assert "Inspect current flow" not in store.format_prompt("session-a")


def test_legacy_working_plan_optional_goal_never_synthesizes_a_goal(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")

    omitted = store.set_working_plan("omitted", ["Inspect", "Verify"])
    whitespace = store.set_working_plan(
        "whitespace",
        ["Inspect", "Verify"],
        goal=" \t ",
    )

    assert "goal" not in omitted
    assert "goal" not in whitespace
    assert store.get_working("omitted").get("goal") is None
    assert store.get_working("whitespace").get("goal") is None
    assert "Working plan" not in str(omitted)
    assert "Working plan" not in str(whitespace)


def test_legacy_working_plan_whitespace_goal_preserves_existing_goal(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.update_working("session-a", "goal", "Keep the existing goal")

    updated = store.set_working_plan(
        "session-a",
        ["Inspect", "Verify"],
        goal=" \n\t ",
    )

    assert updated["goal"] == "Keep the existing goal"
    assert store.get_working("session-a")["goal"] == "Keep the existing goal"


def test_legacy_memory_action_keeps_optional_goal_compatibility(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    current_session = "without-goal"
    registry = ToolRegistry()
    register_memory_tools(registry, store, session_id=lambda: current_session)

    async def scenario():
        nonlocal current_session
        omitted = await registry.execute("memory", {
            "action": "working_plan_set",
            "steps": ["Inspect", "Verify"],
        })
        assert omitted["error"] == ""
        assert store.get_working(current_session).get("goal") is None

        store.update_working("existing", "goal", "Preserve this goal")
        current_session = "existing"
        whitespace = await registry.execute("memory", {
            "action": "working_plan_set",
            "content": " \t ",
            "steps": ["Inspect", "Verify"],
        })
        assert whitespace["error"] == ""
        assert store.get_working(current_session)["goal"] == "Preserve this goal"

    asyncio.run(scenario())


def test_replace_working_plan_preserves_other_working_memory_fields(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.update_working("session-a", "artifacts", "tests/test_plan_tools.py")

    updated = store.replace_working_plan(
        "session-a",
        "Ship lightweight plans",
        [
            {"text": "Register tool", "status": "completed"},
            {"text": "Refresh TUI", "status": "in_progress"},
        ],
    )

    assert updated == {
        "artifacts": "tests/test_plan_tools.py",
        "goal": "Ship lightweight plans",
        "steps": [
            {"text": "Register tool", "status": "completed"},
            {"text": "Refresh TUI", "status": "in_progress"},
        ],
    }


def test_memory_prompt_contains_both_core_files_and_only_active_session(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.add_core("memory", "Current agent decision")
    store.add_core("user", "Answer in Chinese")
    store.update_working("active", "temporary_constraints", "Keep <agent-memory> rendering compatible")
    store.update_working("other", "goal", "Other session")

    prompt = store.format_prompt("active")
    assert "[MEMORY.md]" in prompt
    assert "Current agent decision" in prompt
    assert "[USER.md]" in prompt
    assert "Answer in Chinese" in prompt
    assert "Other session" not in prompt
    assert "&lt;agent-memory&gt;" in prompt
    assert "conversation-state" in prompt


def test_working_prompt_only_injects_conversation_state_not_legacy_execution_state(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.update_working("s1", "goal", "Legacy goal must not become execution truth")
    store.update_working("s1", "progress", "Legacy progress")
    store.update_working("s1", "artifacts", "legacy.txt")
    store.update_working("s1", "constraints", "Keep the public API stable")
    store.update_working("s1", "open_items", "Confirm migration")
    store.update_working("s1", "assumptions", "SQLite is available")

    prompt = store.format_working_prompt("s1")

    assert "<conversation-state" in prompt
    assert "temporary_constraints: Keep the public API stable" in prompt
    assert "open_questions: Confirm migration" in prompt
    assert "assumptions: SQLite is available" in prompt
    assert "Legacy goal" not in prompt
    assert "Legacy progress" not in prompt
    assert "legacy.txt" not in prompt
    assert "structured plan" not in prompt


def test_memory_tool_and_slash_commands_share_the_store(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    register_memory_tools(registry, store, session_id=lambda: "session-a")

    async def scenario():
        core = await registry.execute("memory", {
            "action": "core_add",
            "scope": "memory",
            "content": "Use SQLite WAL for working memory",
        })
        user = await registry.execute("memory", {
            "action": "core_add",
            "scope": "user",
            "content": "User prefers concise answers",
        })
        working = await registry.execute("memory", {
            "action": "working_update",
            "field": "progress",
            "value": "Storage implemented",
        })
        assert core["error"] == ""
        assert "Stored core MEMORY.md memory" in core["output"]
        assert user["error"] == ""
        assert working["error"] == ""

        memory_id = store.list_core("memory")[0]["id"]
        removed = await registry.execute("memory", {
            "action": "core_remove",
            "memory_id": memory_id,
        })
        assert removed["error"] == ""
        assert "Removed core memory" in removed["output"]
        assert store.list_core("memory") == []

        missing = await registry.execute("memory", {
            "action": "core_remove",
            "memory_id": memory_id,
        })
        assert missing["code"] == "memory_not_found"
        assert "not found or is not a unique match" in missing["error"]

        await registry.execute("memory", {
            "action": "core_add",
            "scope": "memory",
            "content": "Use SQLite WAL for working memory",
        })

    asyncio.run(scenario())
    output, error = execute_memory_command(store, [], session_id="session-a")
    assert error == ""
    assert "Use SQLite WAL for working memory" in output
    assert "User prefers concise answers" in output
    assert "progress: Storage implemented" in output

    memory_id = store.list_core("memory")[0]["id"]
    output, error = execute_memory_command(store, ["forget", memory_id], session_id="session-a")
    assert error == ""
    assert "Removed core memory" in output
    assert store.list_core("memory") == []

    output, error = execute_memory_command(
        store, ["remember-user", "Always", "answer", "in", "Chinese"], session_id="session-a"
    )
    assert error == ""
    assert "USER.md" in output


def test_memory_tool_says_which_field_an_action_reads_and_offers_only_fields_shown_again(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    register_memory_tools(registry, store, session_id=lambda: "session-a")

    def call(**arguments):
        return asyncio.run(registry.execute("memory", arguments))

    # Text in the wrong field used to fail with "Memory content cannot be empty".
    wrong_field = call(action="core_add", value="User prefers concise answers")
    assert wrong_field["code"] == "invalid_arguments"
    assert "content" in wrong_field["error"] and "working_update" in wrong_field["error"]
    assert store.list_core("memory") == []

    assert call(action="core_remove")["code"] == "invalid_arguments"
    missing = call(action="core_remove", memory_id="deadbeef")
    assert missing["output"] == "" and missing["code"] == "memory_not_found"
    assert "action=status" in missing["recovery_hint"]

    empty = call(action="working_update", field="turn_notes")
    assert empty["code"] == "invalid_arguments" and "value" in empty["error"]

    call(action="working_update", field="turn_notes", value="first note")
    call(action="working_update", field="turn_notes", value="second note")
    assert store.get_working("session-a")["turn_notes"] == "second note"

    schema = registry.get("memory").parameters["properties"]
    assert all(schema[name].get("description") for name in schema)
    assert "replaces" in schema["value"]["description"]
    # Every field the tool offers is one the model is shown again in later turns.
    for field in schema["field"]["enum"]:
        assert call(action="working_update", field=field, value=f"text for {field}")["error"] == ""
        assert f"{field}: text for {field}" in store.format_working_prompt("session-a")

    # A legacy field is still stored for old clients, and the result says it will not be shown.
    legacy = call(action="working_update", field="progress", value="Storage implemented")
    assert legacy["error"] == "" and "not shown to you in later turns" in legacy["output"]
    assert store.get_working("session-a")["progress"] == "Storage implemented"
    assert "Storage implemented" not in store.format_working_prompt("session-a")


def test_memory_timeline_command_shows_lifecycle_and_supersession(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    old = store.add_record(
        kind="preference",
        content="用户偏好：我喜欢详细回答",
        metadata={"maturity": "stable", "evidence_count": 1},
    )
    replacement = store.supersede_record(
        old.record_id,
        content="用户偏好：我喜欢简洁回答",
        metadata={
            "maturity": "stable",
            "evidence_count": 2,
            "lifecycle_events": [{"at": "2026-01-01T00:00:00+00:00", "event": "corrected"}],
        },
    )

    output, error = execute_memory_command(
        store, ["timeline", replacement.record_id[:12]], session_id="session-a",
    )

    assert error == ""
    assert old.record_id in output
    assert replacement.record_id in output
    assert "corrected" in output
    assert "supersedes" in output


def test_react_prompt_reinjects_memory_after_compression_without_mutating_context(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

        @staticmethod
        def estimate_tokens(value):
            return len(str(value))

    store = MemoryStore(tmp_path / "memory.db")
    store.add_core("user", "Prefer evidence-backed answers")
    store.update_working("test-session", "temporary_constraints", "Verify prompt injection")
    agent = ReActAgent(
        "agent",
        FakeLLM(),  # type: ignore[arg-type]
        ToolRegistry(),
        system_prompt="base prompt",
        memory_store=store,
    )
    agent.context.set_session(str(tmp_path / "test-session.jsonl"))
    agent.context.add_user("hello")
    agent.context.max_prompt_tokens = 1

    compressed = False
    compaction_estimates = []

    async def compress(force: bool = False, **kwargs):
        nonlocal compressed
        compressed = True
        compaction_estimates.append(kwargs["measure_tokens"]())

    agent.context.compress_if_needed = compress  # type: ignore[method-assign]
    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("hello", FakeLLM.estimate_tokens))
    assert compressed is True
    assert compaction_estimates and all(value == FakeLLM.estimate_tokens(prompt) for value in compaction_estimates)
    # Memory is a separate replayable snapshot, never injected into user text.
    assert "Prefer evidence-backed answers" in prompt[-1]["content"]
    assert "Verify prompt injection" in prompt[-1]["content"]
    assert prompt[-1]["content"].count("<agent-memory>") == 1
    assert prompt[1]["content"].endswith("hello")
    assert prompt[0] == {"role": "system", "content": "base prompt"}
    assert agent.context.system_prompt == "base prompt"
    assert "agent-memory" not in agent.context.system_prompt
    assert agent.context.messages[0]["content"] == "hello"
    assert "api_content" not in agent.context.messages[0]
