import asyncio
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from agent.cli.legacy_learning_commands import execute_learning_command
from agent.runtime.learning import LearningReviewer, LearningStore, format_review_outcome
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.skills import register_skill_tools

SKILL_TEXT = """---
name: search-debugging
description: Diagnose search provider failures and retry loops.
---

# Search debugging

## Workflow

1. Inspect the provider selection.
"""


def test_learning_store_connection_closes_after_context(tmp_path):
    store = LearningStore(tmp_path / "learning.db")

    with store._connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")

SUPERPOWERS_TEXT = """---
name: superpowers
description: Select and follow a safe coding workflow before changing code.
---

# Superpowers

Choose Light, Debug, or Full before modifying code.
"""


def test_skill_store_create_list_patch_support_file_and_restore(tmp_path):
    store = SkillStore(tmp_path / "skills")
    created = store.create("search-debugging", SKILL_TEXT)
    assert created["action"] == "created"
    assert store.list()[1:] == [{
        "name": "search-debugging",
        "description": "Diagnose search provider failures and retry loops.",
        "files": 1,
        "category": "general",
        "origin": "user",
    }]
    before = store.raw_file("search-debugging", "SKILL.md")
    store.patch("search-debugging", "Inspect the provider selection.", "Inspect provider selection and credentials.")
    assert "credentials" in store.view("search-debugging")
    store.write_file("search-debugging", "references/exa.md", "# Exa\n")
    assert store.view("search-debugging", "references/exa.md") == "# Exa\n"
    store.restore_file("search-debugging", "SKILL.md", before)
    assert "credentials" not in store.view("search-debugging")

    with pytest.raises(ValueError, match="stay inside"):
        store.view("search-debugging", "../secret.txt")
    with pytest.raises(ValueError, match="NTFS ADS"):
        store.write_file("search-debugging", "scripts/helper.py:payload", "hidden")


def test_skill_store_supports_one_level_categories_without_breaking_name_lookup(tmp_path):
    store = SkillStore(tmp_path / "skills")
    created = store.create("search-debugging", SKILL_TEXT, "coding")

    assert created["category"] == "coding"
    assert (tmp_path / "skills" / "coding" / "search-debugging" / "SKILL.md").is_file()
    assert store.view("search-debugging") == SKILL_TEXT
    assert store.list()[1:] == [{
        "name": "search-debugging",
        "description": "Diagnose search provider failures and retry loops.",
        "files": 1,
        "category": "coding",
        "origin": "user",
    }]
    assert "[coding]" in store.catalog_prompt()


@pytest.mark.parametrize("block", [">", "|", ">-", "|2"])
def test_skill_frontmatter_folds_multiline_values_into_one_line(block):
    header = SkillStore._frontmatter(
        "---\n"
        "name: graph-rag\n"
        f"description: {block}\n"
        "  Use when running GraphRAG\n"
        "\n"
        "  with a local model: extract, build, query.\n"
        "category: operations\n"
        "---\n# Graph RAG\n"
    )
    assert header == {
        "name": "graph-rag",
        "description": "Use when running GraphRAG with a local model: extract, build, query.",
        "category": "operations",
    }


def test_skill_frontmatter_keeps_single_line_values_and_joins_wrapped_ones():
    header = SkillStore._frontmatter(
        '---\nname: "search-debugging"\ndescription: "Diagnose search\n  provider failures."\n---\n'
    )
    assert header == {"name": "search-debugging", "description": "Diagnose search provider failures."}
    with pytest.raises(ValueError, match="requires name and description"):
        SkillStore._frontmatter("---\nname: empty\ndescription: >\n---\n")


def test_learning_reviewer_discards_session_provenance_skill(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"new skill","proposals":[
                  {"kind":"skill_create","name":"session-specific-triage",
                   "content":"---\\nname: session-specific-triage\\ndescription: The conversation demonstrated a one-off fix.\\n---\\n\\n# Triage\\n\\n1. Check the current log.",
                   "reason":"The conversation demonstrated a reusable workflow"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "刚刚排查了一个临时问题"}],
        "session-a",
    ))

    assert proposals == []
    assert learning.list() == []


def test_skill_tools_expose_catalog_and_safe_management(tmp_path):
    store = SkillStore(tmp_path / "skills")
    registry = ToolRegistry()
    register_skill_tools(registry, store)

    async def scenario():
        created = await registry.execute("skill_manage", {
            "action": "create",
            "origin": "auto",
            "name": "search-debugging",
            "content": SKILL_TEXT,
        })
        listed = await registry.execute("skills_list", {})
        viewed = await registry.execute("skill_view", {"name": "search-debugging"})
        assert created["error"] == ""
        assert "search-debugging" in listed["output"]
        assert "# Search debugging" in viewed["output"]

    asyncio.run(scenario())


def test_react_injects_skill_catalog_but_not_full_skill_body(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

        @staticmethod
        def estimate_tokens(value):
            return len(str(value))

    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), system_prompt="base", skill_store=skills)  # type: ignore[arg-type]
    agent.context.add_user("debug search")
    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("debug search", FakeLLM.estimate_tokens))
    assert "search-debugging: Diagnose search provider failures" in prompt[0]["content"]
    assert "Inspect the provider selection" not in prompt[0]["content"]


def test_react_refreshes_skill_catalog_after_runtime_skill_create(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    skills = SkillStore(tmp_path / "skills")
    registry = ToolRegistry()
    register_skill_tools(registry, skills)
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base", skill_store=skills)  # type: ignore[arg-type]
    assert "superpowers" not in agent._available_skill_names

    async def scenario():
        created = await agent._execute_tool_call({
            "id": "create-skill",
            "name": "skill_manage",
            "arguments": json.dumps({
                "action": "create",
                "origin": "auto",
                "name": "superpowers",
                "content": SUPERPOWERS_TEXT,
            }),
        })
        assert created["error"] == ""

    asyncio.run(scenario())
    assert "superpowers" in agent._available_skill_names
    assert "superpowers: Select and follow a safe coding workflow" in agent.context.effective_system_prompt


def test_skill_tool_surface_keeps_one_plan_approval_without_checkpoints(tmp_path):
    skills = SkillStore(tmp_path / "skills")
    registry = ToolRegistry()
    register_skill_tools(registry, skills)
    assert "workflow_checkpoint" not in registry.tool_names
    assert "workflow_design_approval" in registry.tool_names
    workflow_schema = registry.get("workflow_design_approval").parameters  # type: ignore[union-attr]
    assert workflow_schema["required"] == ["summary"]
    assert set(workflow_schema["properties"]) == {"summary", "reason"}

    requests = []

    async def approve(request):
        requests.append(request)
        return "session"

    registry.set_approval_handler(approve)

    async def scenario():
        fallback = await registry.execute(
            "workflow_design_approval",
            {"summary": "Change the public API with a migration and targeted rollback tests."},
            execution_origin="model",
        )
        assert fallback["error"] == ""
        assert requests[-1]["reason_source"] == "backend"
        assert "agent_reason" not in requests[-1]

        result = await registry.execute(
            "workflow_design_approval",
            {
                "summary": "Change the public API with a migration and targeted rollback tests.",
                "reason": "是否要批准这份 API 设计，以便实施迁移并运行回滚测试？",
            },
        )
        assert result["error"] == ""

    asyncio.run(scenario())
    assert len(requests) == 2
    assert requests[0]["kind"] == "workflow_design"
    assert requests[0]["reason_source"] == "backend"
    assert "agent_reason" not in requests[0]
    assert requests[1]["agent_reason"] == "是否要批准这份 API 设计，以便实施迁移并运行回滚测试？"
    assert requests[1]["reason_source"] == "agent"
    assert "workflow_design_approval" not in registry.policy.allowed_tools


def test_execution_does_not_certify_a_source_change(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    registry = ToolRegistry()

    async def edit_probe(path=""):
        return path

    async def execute_probe(command=""):
        return {"exit_code": 0, "stdout": ""}

    registry.register(ToolDef(
        name="edit_probe",
        description="test mutation",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        fn=edit_probe,
        risk="write",
        group="files",
    ))
    registry.register(ToolDef(
        name="execute_probe",
        description="test verification",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        fn=execute_probe,
        risk="execute",
        group="code",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base")  # type: ignore[arg-type]

    async def scenario():
        written = await agent._execute_tool_call({
            "id": "write",
            "name": "edit_probe",
            "arguments": '{"path":"agent/runtime/probe.py"}',
        })
        assert written["error"] == ""
        assert written["coding_change_journal"] == ["agent/runtime/probe.py"]
        assert agent._verification_required is True

        checked = await agent._execute_tool_call({
            "id": "check",
            "name": "execute_probe",
            "arguments": '{"command":"pytest tests/test_probe.py -q"}',
        })
        assert checked["error"] == ""
        assert agent._verification_required is True

    asyncio.run(scenario())








def test_verification_gate_arms_only_for_source_and_readback_does_not_certify(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    registry = ToolRegistry()

    async def edit_probe(path=""):
        return path

    async def read_probe(path=""):
        return path

    registry.register(ToolDef(
        name="edit_probe",
        description="test mutation",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        fn=edit_probe,
        risk="write",
        group="files",
    ))
    registry.register(ToolDef(
        name="read_file",
        description="test readback",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        fn=read_probe,
        risk="read",
        group="files",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base")  # type: ignore[arg-type]

    async def scenario():
        # B: a non-source write is journaled but does NOT arm the gate.
        doc = await agent._execute_tool_call({
            "id": "w1",
            "name": "edit_probe",
            "arguments": '{"path":"memory-archive/notes.md"}',
        })
        assert doc["error"] == ""
        assert doc["coding_change_journal"] == ["memory-archive/notes.md"]
        assert agent._verification_required is False

        # B: a source write arms the gate.
        src = await agent._execute_tool_call({
            "id": "w2",
            "name": "edit_probe",
            "arguments": '{"path":"agent/runtime/probe.py"}',
        })
        assert src["error"] == ""
        assert agent._verification_required is True

        # C: reading back an unrelated path does not clear the gate.
        miss = await agent._execute_tool_call({
            "id": "r1",
            "name": "read_file",
            "arguments": '{"path":"agent/runtime/other.py"}',
        })
        assert miss["error"] == ""
        assert agent._verification_required is True

        # C: reading back the mutated source path clears the gate.
        hit = await agent._execute_tool_call({
            "id": "r2",
            "name": "read_file",
            "arguments": '{"path":"agent/runtime/probe.py"}',
        })
        assert hit["error"] == ""
        assert agent._verification_required is True

    asyncio.run(scenario())


def test_react_blocks_direct_source_rewrite_through_python(tmp_path):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    calls = []
    skills = SkillStore(tmp_path / "skills")
    skills.create("superpowers", SUPERPOWERS_TEXT)
    skills.write_file("superpowers", "references/light-workflow.md", "# Light\n")
    registry = ToolRegistry()
    register_skill_tools(registry, skills)

    async def execute_python(code=""):
        calls.append(code)
        return "done"

    registry.register(ToolDef(
        name="execute_python",
        description="test execution",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        fn=execute_python,
        risk="execute",
        group="code",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base", skill_store=skills)  # type: ignore[arg-type]

    async def scenario():
        blocked = await agent._execute_tool_call({
            "id": "python-write",
            "name": "execute_python",
            "arguments": json.dumps({
                "code": 'open("agent/runtime/tools/browser.py", "w").write("unsafe")',
            }),
        })
        assert blocked["error_type"] == "source_mutation_requires_file_tool"
        assert blocked["details"] == {
            "reason": "Python open(..., write-mode)",
            "targets": ["agent/runtime/tools/browser.py"],
        }
        assert "agent/runtime/tools/browser.py" in blocked["error"]
        assert calls == []

    asyncio.run(scenario())


def test_react_shell_mutation_checks_the_write_target_not_source_arguments():
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    calls = []
    registry = ToolRegistry()

    async def execute_shell(command=""):
        calls.append(command)
        return "tests passed"

    registry.register(ToolDef(
        name="execute_shell",
        description="test execution",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        fn=execute_shell,
        risk="execute",
        group="code",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base")  # type: ignore[arg-type]

    async def scenario():
        allowed = await agent._execute_tool_call({
            "id": "test-log",
            "name": "execute_shell",
            "arguments": json.dumps({
                "command": "pytest tests/test_probe.py > reports/test-results.txt",
            }),
        })
        assert allowed["error"] == ""
        assert calls == ["pytest tests/test_probe.py > reports/test-results.txt"]

    asyncio.run(scenario())


def test_react_shell_mutation_error_names_rule_and_target():
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    calls = []
    registry = ToolRegistry()

    async def execute_shell(command=""):
        calls.append(command)
        return "done"

    registry.register(ToolDef(
        name="execute_shell",
        description="test execution",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        fn=execute_shell,
        risk="execute",
        group="code",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base")  # type: ignore[arg-type]

    async def scenario():
        blocked = await agent._execute_tool_call({
            "id": "powershell-write",
            "name": "execute_shell",
            "arguments": json.dumps({
                "command": "'unsafe' | Set-Content -LiteralPath agent/runtime/probe.py",
            }),
        })
        assert blocked["error_type"] == "source_mutation_requires_file_tool"
        assert blocked["details"] == {
            "reason": "PowerShell set-content",
            "targets": ["agent/runtime/probe.py"],
        }
        assert "PowerShell set-content" in blocked["error"]
        assert "agent/runtime/probe.py" in blocked["recovery_hint"]
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("formatter_fails", [False, True])
def test_react_tracks_formatter_changes_in_coding_journal(formatter_fails):
    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    registry = ToolRegistry()
    execution_options = []

    async def execute_shell(command="", foreground_yield_ms=10_000, background=False):
        execution_options.append((foreground_yield_ms, background))
        if formatter_fails:
            raise RuntimeError("formatter stopped after writing")
        return "1 file reformatted"

    registry.register(ToolDef(
        name="execute_shell",
        description="test execution",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "foreground_yield_ms": {"type": "integer"},
                "background": {"type": "boolean"},
            },
            "required": ["command"],
        },
        fn=execute_shell,
        risk="execute",
        group="code",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, system_prompt="base")  # type: ignore[arg-type]
    snapshots = iter([
        {"agent/runtime/preexisting.py": "file:same"},
        {
            "agent/runtime/preexisting.py": "file:same",
            "agent/runtime/probe.py": "file:new",
        },
    ])

    async def snapshot():
        return next(snapshots)

    agent._git_worktree_snapshot = snapshot  # type: ignore[method-assign]

    async def scenario():
        formatted = await agent._execute_tool_call({
            "id": "format",
            "name": "execute_shell",
            "arguments": json.dumps({"command": "ruff format agent/runtime/probe.py"}),
        })
        assert bool(formatted["error"]) is formatter_fails
        assert formatted["coding_change_journal"] == ["agent/runtime/probe.py"]
        assert "preexisting.py" not in formatted["tool_output"]
        assert execution_options == [(0, False)]
        assert agent._verification_required is True

    asyncio.run(scenario())


def test_git_worktree_snapshot_detects_further_changes_to_dirty_file(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "agent" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    agent = ReActAgent(
        "agent",
        FakeLLM(),
        ToolRegistry(),
        system_prompt="base",
    )  # type: ignore[arg-type]
    agent._sandbox = SimpleNamespace(workdir=str(repo))

    async def scenario():
        before = await agent._git_worktree_snapshot()
        assert before is not None
        assert "agent/probe.py" in before

        source.write_text("second\n", encoding="utf-8")
        after = await agent._git_worktree_snapshot()
        assert after is not None
        assert agent._changed_snapshot_paths(before, after) == {"agent/probe.py"}

    asyncio.run(scenario())










def test_learning_proposal_approval_and_rollback_for_memory_and_skill(tmp_path):
    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)

    memory_proposal = learning.stage(
        "session-a", "memory", {"scope": "user", "content": "User prefers concise Chinese answers"}, "User correction"
    )
    applied_memory = learning.approve(memory_proposal["id"], memory, skills)
    assert applied_memory["status"] == "applied"
    assert memory.list_core("user")[0]["content"] == "User prefers concise Chinese answers"
    assert learning.rollback(memory_proposal["id"], memory, skills)["status"] == "rolled_back"
    assert memory.list_core("user") == []

    patch_proposal = learning.stage(
        "session-a",
        "skill_patch",
        {
            "name": "search-debugging",
            "file_path": "SKILL.md",
            "old_string": "Inspect the provider selection.",
            "new_string": "Inspect the provider selection and active credentials.",
        },
        "Verified debugging improvement",
    )
    assert "active credentials" in learning.format_detail(patch_proposal["id"], skills)
    learning.approve(patch_proposal["id"], memory, skills)
    assert "active credentials" in skills.view("search-debugging")
    learning.rollback(patch_proposal["id"], memory, skills)
    assert "active credentials" not in skills.view("search-debugging")

    additive_patch = learning.stage(
        "session-a",
        "skill_patch",
        {
            "name": "search-debugging",
            "file_path": "SKILL.md",
            "old_string": "Inspect the provider selection.",
            "new_string": "Inspect the provider selection. Then verify the configured endpoint.",
        },
        "Add a verified follow-up without removing the existing step",
    )
    applied_additive = learning.apply_additive(additive_patch["id"], memory, skills)
    assert applied_additive["status"] == "applied"
    assert "Then verify the configured endpoint" in skills.view("search-debugging")


def test_learning_reviewer_stages_evidence_backed_memory_for_explicit_user_choice(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            assert "Existing skill catalog" in messages[1]["content"]
            return {
                "content": """```json
                {"summary":"one preference","proposals":[
                  {"kind":"memory","scope":"user","content":"User prefers tables for comparisons",
                   "evidence":"比较时请用表格","reason":"Explicit preference"}
                ]}
                ```"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)
    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "比较时请用表格"}, {"role": "assistant", "content": "好的"}],
        "session-a",
    ))
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending"
    assert memory.list_core("user") == []
    learning.approve(proposals[0]["id"], memory, skills)
    assert memory.list_core("user")[0]["content"] == "User prefers tables for comparisons"

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["status"],
        session_id="session-a",
        messages=[],
    ))
    assert error == ""
    assert "isolated candidates" in output


def test_learning_reviewer_keeps_user_evidence_observation_out_of_recall_until_approved(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": json.dumps({
                    "summary": "project decision",
                    "proposals": [{
                        "kind": "observation",
                        "content": "Astra uses lightweight local memory on macOS without Hindsight.",
                        "evidence": "mac上就不用重的hindsight了",
                        "evidence_role": "user",
                        "tags": ["astra", "memory", "macos"],
                        "reason": "Explicit project architecture decision",
                    }],
                }),
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    reviewer = LearningReviewer(
        FakeLLM(), learning, memory, SkillStore(tmp_path / "skills")
    )

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "mac上就不用重的hindsight了"}],
        "session-a",
    ))

    assert len(proposals) == 1
    assert proposals[0]["kind"] == "observation"
    assert proposals[0]["status"] == "pending"
    assert memory.recall_records("lightweight local memory", kinds=("observation",)) == []
    learning.approve(proposals[0]["id"], memory, reviewer.skills)
    records = memory.recall_records("lightweight local memory", kinds=("observation",))
    assert len(records) == 1
    assert records[0].metadata["evidence_role"] == "user"
    assert records[0].metadata["maturity"] == "confirmed"
    assert records[0].metadata["ttl_days"] == 90
    assert records[0].valid_until
    assert set(records[0].tags) == {"astra", "memory", "macos"}

    learning.rollback(proposals[0]["id"], memory, reviewer.skills)
    assert memory.recall_records("lightweight local memory", kinds=("observation",)) == []


def test_learning_reviewer_accepts_exact_tool_observation_and_rejects_assistant_claim(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": json.dumps({
                    "summary": "verification",
                    "proposals": [
                        {
                            "kind": "observation",
                            "content": "The focused sandbox suite passed.",
                            "evidence": "35 passed, 3 skipped",
                            "evidence_role": "tool",
                            "reason": "Verified command output",
                        },
                        {
                            "kind": "observation",
                            "content": "The assistant says the deployment is healthy.",
                            "evidence": "deployment is healthy",
                            "evidence_role": "assistant",
                            "reason": "Assistant-only claim",
                        },
                    ],
                }),
            }

    memory = MemoryStore(tmp_path / "memory.db")
    reviewer = LearningReviewer(
        FakeLLM(),
        LearningStore(tmp_path / "learning.db"),
        memory,
        SkillStore(tmp_path / "skills"),
    )
    proposals = asyncio.run(reviewer.review(
        [
            {"role": "tool", "content": "pytest output: 35 passed, 3 skipped"},
            {"role": "assistant", "content": "deployment is healthy"},
        ],
        "session-a",
    ))

    assert len(proposals) == 1
    assert proposals[0]["payload"]["evidence_role"] == "tool"
    assert proposals[0]["status"] == "pending"
    assert memory.recall_records("sandbox suite", kinds=("observation",)) == []


def test_repeated_learning_observation_reuses_candidate_without_corroboration(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": json.dumps({
                    "summary": "same decision",
                    "proposals": [{
                        "kind": "observation",
                        "content": "Astra keeps Hindsight disabled on macOS.",
                        "evidence": "macOS 不启用 Hindsight",
                        "evidence_role": "user",
                        "reason": "Repeated architecture decision",
                    }],
                }),
            }

    memory = MemoryStore(tmp_path / "memory.db")
    reviewer = LearningReviewer(
        FakeLLM(),
        LearningStore(tmp_path / "learning.db"),
        memory,
        SkillStore(tmp_path / "skills"),
    )
    messages = [{"role": "user", "content": "macOS 不启用 Hindsight"}]

    first = asyncio.run(reviewer.review(messages, "session-a"))
    second = asyncio.run(reviewer.review_outcome(messages, "session-b"))

    records = memory.recall_records("Hindsight disabled", kinds=("observation",))
    assert records == []
    assert second["proposals"] == []
    assert first[0]["id"] == second["unchanged"][0]["id"]
    assert len(second["unchanged"][0]["learning"]["sources"]) == 1


def test_review_outcome_reports_learning_layers():
    output = format_review_outcome({
        "summary": "layered learning",
        "proposals": [
            {"id": "lr_core", "kind": "memory", "status": "applied"},
            {"id": "lr_obs", "kind": "observation", "status": "applied"},
            {"id": "lr_skill", "kind": "skill_create", "status": "pending", "reason": "collision"},
        ],
    })

    assert "Core memory: 1 applied" in output
    assert "Observations: 1 applied" in output
    assert "Skills: 0 applied" in output


def test_observation_normalization_bounds_content_and_evidence():
    content = "c" * 5000
    evidence = "e" * 1200

    normalized = LearningReviewer._normalize(
        {
            "kind": "observation",
            "content": content,
            "evidence": evidence,
            "evidence_role": "tool",
        },
        tool_messages=(evidence,),
    )

    assert normalized is not None
    assert len(normalized["payload"]["content"]) == 4000
    assert len(normalized["payload"]["evidence"]) == 1000


def test_learning_reviewer_stages_bounded_skill_rewrite_without_overwriting_baseline(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"simplify workflow","proposals":[
                  {"kind":"skill_patch","name":"search-debugging","file_path":"SKILL.md",
                   "old_string":"Inspect the provider selection.","new_string":"Inspect credentials.",
                   "reason":"Shorter workflow"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    proposals = asyncio.run(reviewer.review(
        [{"role": "tool", "content": SKILL_TEXT}, {"role": "assistant", "content": "I can simplify it"}],
        "session-a",
    ))

    assert proposals[0]["status"] == "pending"
    assert "Inspect the provider selection." in skills.view("search-debugging")
    learning.approve(proposals[0]["id"], memory, skills)
    assert "Inspect credentials." in skills.view("search-debugging")
    learning.rollback(proposals[0]["id"], memory, skills)
    assert "Inspect the provider selection." in skills.view("search-debugging")


def test_learning_reviewer_repairs_candidate_frontmatter_without_installing(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"local PDF workflow","proposals":[
                  {"kind":"skill_create","name":"private-local-pdf-inspection",
                   "content":"# Private Local PDF Inspection\\n\\n1. Extract text locally.\\n2. Render scanned pages.",
                   "reason":"Reusable private document workflow"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "以后敏感 PDF 都只在本地检查"}],
        "session-a",
    ))

    assert proposals[0]["status"] == "pending"
    assert [item for item in skills.list() if item["category"] != "builtin"] == []
    content = proposals[0]["payload"]["content"]
    assert content.startswith("---\n")
    assert 'name: "private-local-pdf-inspection"' in content
    assert 'description: "Reusable private document workflow"' in content
    assert "# Private Local PDF Inspection" in content


def test_learning_reviewer_normalizes_generated_skill_name_and_frontmatter(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"sandbox verification","proposals":[
                  {"kind":"skill_create","name":"verify_dev_sandbox_image",
                   "content":"---\\nname: verify_dev_sandbox_image\\ndescription: Verify a rebuilt sandbox image.\\n---\\n\\n# Verify sandbox image\\n\\n1. Run the offline checks.",
                   "reason":"Reusable sandbox verification workflow"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "以后重建沙箱后都跑离线验收"}],
        "session-a",
    ))

    assert proposals[0]["status"] == "pending"
    assert proposals[0]["payload"]["name"] == "verify-dev-sandbox-image"
    content = proposals[0]["payload"]["content"]
    assert SkillStore._frontmatter(content)["name"] == "verify-dev-sandbox-image"


@pytest.mark.parametrize("invalid_name", ["___", None, "a" * 65])
def test_learning_reviewer_rejects_empty_normalized_skill_name(tmp_path, invalid_name):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": json.dumps({
                    "summary": "invalid skill",
                    "proposals": [{
                        "kind": "skill_create",
                        "name": invalid_name,
                        "content": "# Invalid skill\n\n1. Do not create this skill.",
                        "reason": "No usable ASCII name",
                    }],
                })
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "不要创建无效名称"}],
        "session-a",
    ))

    assert proposals == []
    assert learning.list() == []
    assert [item for item in skills.list() if item["category"] != "builtin"] == []


def test_learning_reviewer_keeps_major_reduction_pending_with_reason(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"reduce workflow","proposals":[
                  {"kind":"skill_patch","name":"search-debugging","file_path":"SKILL.md",
                   "old_string":"Inspect the provider selection.","new_string":"Check.",
                   "reason":"Shorter workflow"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    outcome = asyncio.run(reviewer.review_outcome(
        [{"role": "tool", "content": SKILL_TEXT}, {"role": "assistant", "content": "I can reduce it"}],
        "session-a",
    ))

    proposal = outcome["proposals"][0]
    assert proposal["status"] == "pending"
    assert learning.requires_approval(proposal, skills)
    assert "Inspect the provider selection." in skills.view("search-debugging")
    rendered = format_review_outcome(outcome)
    assert "unverified learning candidate" in rendered
    assert proposal["id"] in rendered


def test_manual_learning_summary_runs_when_auto_mode_is_off(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            assert "explicitly requested" in messages[0]["content"]
            return {
                "content": """{"summary":"User chose concise Chinese output.","proposals":[
                  {"kind":"memory","scope":"user","content":"User prefers concise Chinese output",
                   "evidence":"以后简洁中文回答","reason":"Explicit preference"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    learning.set_mode("off")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["summarize"],
        session_id="session-a",
        messages=[{"role": "user", "content": "以后简洁中文回答"}],
    ))

    assert error == ""
    assert "Learning summary: User chose concise Chinese output." in output
    assert "unverified learning candidate" in output
    assert memory.list_core("user") == []


def test_learning_command_bulk_approve_and_reject_continue_after_failures(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {"content": '{"summary":"","proposals":[]}'}

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, skills)

    valid = learning.stage(
        "session-a",
        "memory",
        {"scope": "user", "content": "User prefers concise output", "evidence": "简洁回答"},
        "Explicit preference",
    )
    invalid = learning.stage(
        "session-a",
        "skill_create",
        {"name": "broken-skill", "content": "# Missing frontmatter"},
        "Malformed historical proposal",
    )

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["approve", "all"],
        session_id="session-a",
        messages=[],
    ))

    assert error == ""
    assert "1 applied, 1 failed" in output
    assert learning.get(valid["id"])["status"] == "applied"
    assert learning.get(invalid["id"])["status"] == "pending"
    assert f"Failed: {invalid['id']}" in output
    assert "Remaining pending: 1" in output

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["reject-all"],
        session_id="session-a",
        messages=[],
    ))

    assert error == ""
    assert "1 rejected, 0 failed" in output
    assert learning.get(invalid["id"])["status"] == "rejected"
    assert learning.list("pending", limit=10_000) == []


def test_learning_command_repairs_pending_proposal_with_active_model(tmp_path):
    class RepairLLM:
        async def chat(self, messages):
            assert "Repair one pending learning proposal" in messages[0]["content"]
            assert "skills/search-debugging.md" in messages[1]["content"]
            assert "Inspect the provider selection." in messages[1]["content"]
            return {
                "content": """{"message":"I fixed the legacy skill path and kept the exact append anchor.","proposal":{
                  "kind":"skill_patch",
                  "name":"search-debugging",
                  "file_path":"SKILL.md",
                  "old_string":"Inspect the provider selection.",
                  "new_string":"Inspect the provider selection. Then verify the configured endpoint.",
                  "reason":"Repair the legacy path and append with an exact anchor"
                }}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    reviewer = LearningReviewer(RepairLLM(), learning, memory, skills)
    original = learning.stage(
        "session-old",
        "skill_patch",
        {
            "name": "search-debugging",
            "file_path": "skills/search-debugging.md",
            "old_string": "",
            "new_string": "Then verify the configured endpoint.",
        },
        "Malformed historical proposal",
    )

    progress = []
    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["repair", original["id"]],
        session_id="session-current",
        messages=[],
        on_progress=progress.append,
    ))

    assert error == ""
    assert progress == [
        f"I’m repairing learning proposal `{original['id']}` with the active model. "
        "I’ll preserve its intent and only change what is needed to satisfy the current rules.",
        "\n\nI fixed the legacy skill path and kept the exact append anchor.",
    ]
    repaired_original = learning.get(original["id"])
    assert repaired_original["status"] == "superseded"
    replacement_id = repaired_original["result"]["replacement_id"]
    assert learning.get(replacement_id)["status"] == "applied"
    assert learning.get(replacement_id)["session_id"] == "session-old"
    assert f"{original['id']} -> {replacement_id}" in output
    assert "Then verify the configured endpoint." in skills.view("search-debugging")
    assert f"Replacement: {replacement_id}" in learning.format_detail(original["id"], skills)


def test_learning_repair_accepts_nested_payload_and_locks_skill_name(tmp_path):
    class RepairLLM:
        async def chat(self, messages):
            return {
                "content": """{"message":"I moved the reusable provider check into the skill references.","proposal":{
                  "kind":"skill_write_file",
                  "payload":{
                    "name":"different-skill",
                    "file_path":"references/provider-check.md",
                    "content":"# Provider check\\n\\nVerify the configured endpoint."
                  },
                  "reason":"Move the reusable note under references"
                }}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    reviewer = LearningReviewer(RepairLLM(), learning, memory, skills)
    original = learning.stage(
        "session-old",
        "skill_write_file",
        {
            "name": "search-debugging",
            "file_path": "provider-check.md",
            "content": "Verify the configured endpoint.",
        },
        "Legacy root supporting file",
    )

    outcome = asyncio.run(reviewer.repair(original["id"]))

    replacement = outcome["replacement"]
    assert replacement["payload"]["name"] == "search-debugging"
    assert skills.raw_file("search-debugging", "references/provider-check.md")
    assert all(item["name"] != "different-skill" for item in skills.list())


def test_learning_repair_normalizes_and_locks_historical_invalid_skill_name(tmp_path):
    class RepairLLM:
        async def chat(self, messages):
            return {
                "content": """{"message":"I repaired the sandbox verification workflow.","proposal":{
                  "kind":"skill_create",
                  "name":"different-skill",
                  "content":"---\\nname: different-skill\\ndescription: Verify a rebuilt sandbox image.\\n---\\n\\n# Verify sandbox image\\n\\n1. Run the offline checks.",
                  "reason":"Repair the reusable sandbox verification workflow"
                }}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(RepairLLM(), learning, memory, skills)
    original = learning.stage(
        "session-old",
        "skill_create",
        {
            "name": "verify_dev_sandbox_image",
            "content": "# Verify sandbox image\n\n1. Run the offline checks.",
        },
        "Malformed historical proposal",
    )

    outcome = asyncio.run(reviewer.repair(original["id"]))

    replacement = outcome["replacement"]
    assert replacement["status"] == "applied"
    assert replacement["payload"]["name"] == "verify-dev-sandbox-image"
    content = skills.view("verify-dev-sandbox-image")
    assert SkillStore._frontmatter(content)["name"] == "verify-dev-sandbox-image"
    assert all(item["name"] != "different-skill" for item in skills.list())
    assert learning.get(original["id"])["status"] == "superseded"
    assert "verify_dev_sandbox_image" in outcome["message"]
    assert "verify-dev-sandbox-image" in outcome["message"]


def test_learning_repair_rejects_name_without_slug_before_model_call(tmp_path):
    class RepairLLM:
        calls = 0

        async def chat(self, messages):
            self.calls += 1
            return {"content": '{"proposal":null}'}

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    llm = RepairLLM()
    reviewer = LearningReviewer(llm, learning, memory, skills)
    original = learning.stage(
        "session-old",
        "skill_create",
        {"name": "___", "content": "# Invalid skill\n\n1. Do nothing."},
        "Malformed historical proposal",
    )

    with pytest.raises(ValueError, match="cannot be normalized to a valid skill name"):
        asyncio.run(reviewer.repair(original["id"]))

    assert llm.calls == 0
    assert learning.get(original["id"])["status"] == "pending"
    assert len(learning.list("rejected")) == 0


def test_learning_repair_failure_is_recorded_on_original_pending(tmp_path):
    class RepairLLM:
        async def chat(self, messages):
            return {"content": '{"proposal":null}'}

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    reviewer = LearningReviewer(RepairLLM(), learning, memory, skills)
    original = learning.stage(
        "session-old",
        "skill_patch",
        {
            "name": "search-debugging",
            "file_path": "bad.md",
            "old_string": "",
            "new_string": "Reusable note",
        },
        "Malformed historical proposal",
    )

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["repair", original["id"]],
        session_id="session-current",
        messages=[],
    ))

    assert output == ""
    assert "before a replacement was staged" in error
    pending = learning.get(original["id"])
    assert pending["status"] == "pending"
    assert "Model did not return a valid repair proposal after 2 attempts" in (
        pending["result"]["last_repair_error"]
    )
    detail = learning.format_detail(original["id"], skills)
    assert "Repair attempts:" in detail
    assert "(no replacement staged)" in detail
    assert "Model did not return a valid repair proposal" in detail


def test_learning_command_repair_all_continues_after_model_failure(tmp_path):
    class RepairLLM:
        failing_id = ""

        async def chat(self, messages):
            if self.failing_id in messages[1]["content"]:
                return {"content": '{"proposal":null}'}
            return {
                "content": """{"message":"I moved the reusable provider check into the skill references.","proposal":{
                  "kind":"skill_write_file",
                  "name":"search-debugging",
                  "file_path":"references/provider-check.md",
                  "content":"# Provider check\\n\\nVerify the configured endpoint.",
                  "reason":"Move the reusable note under references"
                }}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    skills.create("search-debugging", SKILL_TEXT)
    llm = RepairLLM()
    reviewer = LearningReviewer(llm, learning, memory, skills)
    valid = learning.stage(
        "session-a",
        "skill_write_file",
        {
            "name": "search-debugging",
            "file_path": "provider-check.md",
            "content": "Verify the configured endpoint.",
        },
        "Legacy root supporting file",
    )
    failing = learning.stage(
        "session-b",
        "skill_patch",
        {
            "name": "search-debugging",
            "file_path": "bad.md",
            "old_string": "",
            "new_string": "Unrepairable in this test",
        },
        "Model failure case",
    )
    llm.failing_id = failing["id"]

    progress = []
    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["repair", "all"],
        session_id="session-current",
        messages=[],
        on_progress=progress.append,
    ))

    assert error == ""
    assert progress[0] == (
        "I found 2 pending learning proposal(s). "
        "I’ll repair them one by one while preserving their original intent."
    )
    assert len(progress[1:]) == 3
    assert any(valid["id"] in message for message in progress[1:])
    assert any(failing["id"] in message for message in progress[1:])
    assert any("(1/2)" in message for message in progress[1:])
    assert any("(2/2)" in message for message in progress[1:])
    assert "\n\nI moved the reusable provider check into the skill references." in progress
    assert "1 applied, 1 failed" in output
    assert learning.get(valid["id"])["status"] == "superseded"
    assert learning.get(failing["id"])["status"] == "pending"
    assert f"Failed: {failing['id']}" in output
    assert skills.raw_file("search-debugging", "references/provider-check.md")


def test_memory_repair_uses_remaining_capacity_and_reasoning_json(tmp_path):
    class RepairLLM:
        def __init__(self):
            self.prompts = []
            self.options = {}

        async def chat_limited(self, messages, **options):
            self.prompts.append(messages)
            self.options = options
            return {
                "content": "",
                "reasoning_content": json.dumps({
                    "message": "I kept the fail-closed rule and shortened it to fit Core Memory.",
                    "proposal": {
                        "content": "Tool claim failure blocks side-effecting tools; only idempotent reads degrade.",
                        "reason": "Keep the durable safety contract concise",
                    },
                }),
                "finish_reason": "stop",
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    memory.memory_char_limit = 150
    memory.add_core("memory", "x" * 50)
    evidence = "写入工具不会执行"
    original = learning.stage(
        "session-a",
        "memory",
        {
            "scope": "memory",
            "content": "A much longer historical tool-claim explanation that no longer fits.",
            "evidence": evidence,
        },
        "Core memory was full",
    )
    llm = RepairLLM()
    reviewer = LearningReviewer(llm, learning, memory, SkillStore(tmp_path / "skills"))

    outcome = asyncio.run(reviewer.repair(original["id"]))

    assert outcome["replacement"]["status"] == "applied"
    repaired = outcome["replacement"]["payload"]
    assert repaired["scope"] == "memory"
    assert repaired["evidence"] == evidence
    assert repaired["content"].startswith("Tool claim failure blocks")
    assert outcome["message"] == (
        "I kept the fail-closed rule and shortened it to fit Core Memory."
    )
    assert "at most 97 content characters" in llm.prompts[0][0]["content"]
    assert llm.options["reasoning_effort"] == "low"


def test_memory_repair_retries_when_first_content_exceeds_capacity(tmp_path):
    class RepairLLM:
        def __init__(self):
            self.prompts = []

        async def chat(self, messages):
            self.prompts.append(messages)
            content = "y" * 100 if len(self.prompts) == 1 else "Tool claims fail closed."
            return {
                "content": json.dumps({
                    "proposal": {
                        "kind": "memory",
                        "content": content,
                        "reason": "Concise durable safety rule",
                    },
                }),
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    memory.memory_char_limit = 120
    memory.add_core("memory", "x" * 50)
    evidence = "写入工具不会执行"
    original = learning.stage(
        "session-a",
        "memory",
        {
            "scope": "memory",
            "content": "Long tool claim behavior",
            "evidence": evidence,
        },
        "Core memory was full",
    )
    llm = RepairLLM()
    reviewer = LearningReviewer(llm, learning, memory, SkillStore(tmp_path / "skills"))

    outcome = asyncio.run(reviewer.repair(original["id"]))

    assert len(llm.prompts) == 2
    assert "exceeded 67 characters" in llm.prompts[1][-1]["content"]
    assert outcome["replacement"]["payload"]["content"] == "Tool claims fail closed."


def test_learning_reviewer_rejects_memory_without_exact_user_evidence(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            return {
                "content": """{"summary":"unsupported inference","proposals":[
                  {"kind":"memory","scope":"user","content":"User prefers detailed answers",
                   "evidence":"I prefer detailed answers","reason":"Unsupported translation"}
                ]}"""
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    reviewer = LearningReviewer(
        FakeLLM(),
        learning,
        memory,
        SkillStore(tmp_path / "skills"),
    )

    proposals = asyncio.run(reviewer.review(
        [
            {"role": "user", "content": "随便聊聊"},
            {"role": "assistant", "content": "I prefer detailed answers"},
        ],
        "session-a",
    ))

    assert proposals == []
    assert memory.list_core("user") == []


def test_learning_mode_and_counter_are_persistent(tmp_path):
    path = tmp_path / "learning.db"
    store = LearningStore(path)
    assert store.mode() == "review"
    assert store.set_mode("off") == "off"
    assert LearningStore(path).mode() == "off"
    assert store.advance_review_counter(2) is False
    assert store.advance_review_counter(2) is True


def test_learning_review_counter_is_isolated_by_session_and_can_reset(tmp_path):
    store = LearningStore(tmp_path / "learning.db")

    assert store.advance_review_counter(2, "session-a") is False
    assert store.advance_review_counter(2, "session-b") is False
    assert store.advance_review_counter(2, "session-a") is True
    assert store.advance_review_counter(2, "session-b") is True

    assert store.advance_review_counter(2, "session-a") is False
    store.reset_review_counter("session-a")
    assert store.advance_review_counter(2, "session-a") is False


def test_learning_transcript_preserves_user_intent_across_tool_heavy_session(monkeypatch):
    monkeypatch.setenv("LEARNING_REVIEW_CONTEXT_CHARS", "12000")
    messages = [{"role": "user", "content": "先按收件箱、垃圾箱、同步状态做三查"}]
    for index in range(20):
        messages.extend([
            {"role": "assistant", "content": f"checking stage {index}"},
            {"role": "tool", "content": f"tool-{index} " + ("x" * 2000)},
        ])
    messages.extend([
        {
            "role": "user",
            "content": "[SYSTEM-SUPPLIED NOTIFICATION] hidden runtime message",
            "provenance": "notification",
        },
        {"role": "user", "content": "最后确认有没有新增邮件"},
    ])

    transcript = LearningReviewer._transcript(messages)

    assert len(transcript) <= 12000
    assert "先按收件箱、垃圾箱、同步状态做三查" in transcript
    assert "最后确认有没有新增邮件" in transcript
    assert "hidden runtime message" not in transcript
    assert "tool-19" in transcript
    assert "tool-0" not in transcript


def test_learning_reviewer_persists_and_reuses_review_summaries_as_hints(tmp_path):
    class FakeLLM:
        def __init__(self):
            self.prompts = []

        async def chat(self, messages):
            self.prompts.append(messages)
            if len(self.prompts) == 1:
                assert "Recent learning summaries (pattern hints only; not evidence): []" in messages[1]["content"]
                return {
                    "content": json.dumps({
                        "summary": "This turn repeated the inbox, spam, and sync-status triage pattern.",
                        "proposals": [],
                    }),
                }
            assert "inbox, spam, and sync-status triage pattern" in messages[1]["content"]
            assert "Use judgment rather than a fixed repetition count" in messages[0]["content"]
            assert "cannot serve as exact evidence" in messages[0]["content"]
            return {
                "content": json.dumps({
                    "summary": "The same mail triage pattern appeared again; keep evaluating its reuse value.",
                    "proposals": [],
                }),
            }

    learning = LearningStore(tmp_path / "learning.db")
    llm = FakeLLM()
    reviewer = LearningReviewer(
        llm,
        learning,
        MemoryStore(tmp_path / "memory.db"),
        SkillStore(tmp_path / "skills"),
    )

    first = asyncio.run(reviewer.review_outcome(
        [{"role": "user", "content": "检查三个邮件入口"}],
        "session-a",
    ))
    second = asyncio.run(reviewer.review_outcome(
        [{"role": "user", "content": "再检查一次邮件"}],
        "session-b",
    ))

    assert first["proposals"] == []
    assert second["proposals"] == []
    summaries = learning.recent_review_summaries()
    assert [item["session_id"] for item in summaries] == ["session-a", "session-b"]
    assert summaries[0]["summary"].startswith("This turn repeated")


def test_learning_reviewer_prompts_model_to_discard_transient_session_memory(tmp_path):
    class FakeLLM:
        async def chat(self, messages):
            assert "transient failures" in messages[0]["content"]
            assert "one-off narratives" in messages[0]["content"]
            return {
                "content": '{"summary":"tool test only","proposals":[]}'
            }

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    reviewer = LearningReviewer(FakeLLM(), learning, memory, SkillStore(tmp_path / "skills"))

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "这次测试工具调用"}],
        "session-a",
    ))

    assert proposals == []
    assert learning.list() == []


def test_learning_reviewer_limits_generation_and_rejects_inferred_image_preferences(tmp_path):
    class FakeLLM:
        def __init__(self):
            self.options = None

        async def chat_limited(self, messages, **options):
            self.options = options
            return {
                "content": """{"summary":"image review","proposals":[
                  {"kind":"memory","scope":"user",
                   "content":"图片审图偏好（叙事解读）：用户喜欢剧情猜测和氛围解读。",
                   "reason":"从最新两张图片推断出的偏好。"}
                ]}"""
            }

    llm = FakeLLM()
    learning = LearningStore(tmp_path / "learning.db")
    reviewer = LearningReviewer(
        llm,
        learning,
        MemoryStore(tmp_path / "memory.db"),
        SkillStore(tmp_path / "skills"),
    )

    proposals = asyncio.run(reviewer.review(
        [{"role": "user", "content": "你猜猜这张图的剧情"}],
        "session-a",
    ))

    assert proposals == []
    assert llm.options == {
        "max_tokens": 2048,
        "temperature": 0.1,
        "disable_thinking": True,
        "request_timeout": 360.0,
        "max_retries": 1,
    }


def test_learning_reviewer_forwards_dedicated_request_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_REVIEW_TIMEOUT", "240")
    monkeypatch.setenv("LEARNING_REVIEW_MAX_RETRIES", "2")

    class FakeLLM:
        async def chat_limited(self, _messages, **options):
            self.options = options
            return {"content": '{"summary":"","proposals":[]}'}

    llm = FakeLLM()
    reviewer = LearningReviewer(
        llm,
        LearningStore(tmp_path / "learning.db"),
        MemoryStore(tmp_path / "memory.db"),
        SkillStore(tmp_path / "skills"),
    )

    asyncio.run(reviewer.review_outcome(
        [{"role": "user", "content": "remember this"}],
        "session-a",
    ))

    assert llm.options["request_timeout"] == 240.0
    assert llm.options["max_retries"] == 2


class LearningSensitiveProviderError(Exception):
    def __init__(self):
        super().__init__(
            "SENSITIVE_SENTINEL Authorization: Bearer provider-secret \x1b[31m"
        )
        self.status_code = 503
        self.request_id = "trace-learning_42"
        self.body = {
            "error": {
                "message": "SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"
            }
        }


class FailingLearningLLM:
    async def chat(self, _messages):
        raise LearningSensitiveProviderError


def _assert_learning_provider_error_is_safe(rendered: str) -> None:
    assert "SENSITIVE_SENTINEL" not in rendered
    assert "provider-secret" not in rendered
    assert "Bearer" not in rendered
    assert "\x1b" not in rendered
    assert "type=LearningSensitiveProviderError" in rendered
    assert "component=learning-review" in rendered
    assert "status=503" in rendered
    assert "request_id=trace-learning_42" in rendered
    assert "timeout=42s" in rendered


def _failing_learning_reviewer(tmp_path):
    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(FailingLearningLLM(), learning, memory, skills)
    return learning, memory, skills, reviewer


def _stage_learning_repair(learning, skills):
    skills.create("search-debugging", SKILL_TEXT)
    return learning.stage(
        "session-a",
        "skill_write_file",
        {
            "name": "search-debugging",
            "file_path": "provider-check.md",
            "content": "Verify the configured endpoint.",
        },
        "Legacy root supporting file",
    )


def test_manual_learning_summary_sanitizes_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_REVIEW_TIMEOUT", "42")
    learning, memory, skills, reviewer = _failing_learning_reviewer(tmp_path)

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["summarize"],
        session_id="session-a",
        messages=[{"role": "user", "content": "summarize this"}],
    ))

    assert output == ""
    _assert_learning_provider_error_is_safe(error)


def test_single_learning_repair_sanitizes_provider_failure_before_persistence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LEARNING_REVIEW_TIMEOUT", "42")
    learning, memory, skills, reviewer = _failing_learning_reviewer(tmp_path)
    original = _stage_learning_repair(learning, skills)

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["repair", original["id"]],
        session_id="session-a",
        messages=[],
    ))

    assert output == ""
    _assert_learning_provider_error_is_safe(error)
    persisted = learning.get(original["id"])["result"]["last_repair_error"]
    _assert_learning_provider_error_is_safe(persisted)
    _assert_learning_provider_error_is_safe(
        learning.format_detail(original["id"], skills)
    )


def test_learning_repair_all_sanitizes_provider_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_REVIEW_TIMEOUT", "42")
    learning, memory, skills, reviewer = _failing_learning_reviewer(tmp_path)
    original = _stage_learning_repair(learning, skills)

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["repair", "all"],
        session_id="session-a",
        messages=[],
    ))

    assert error == ""
    assert f"Failed: {original['id']}" in output
    _assert_learning_provider_error_is_safe(output)


def test_learning_command_keeps_local_validation_and_filesystem_errors_useful(
    tmp_path,
    monkeypatch,
):
    learning, memory, skills, reviewer = _failing_learning_reviewer(tmp_path)

    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["approve", "missing"],
        session_id="session-a",
        messages=[],
    ))
    assert output == ""
    assert error == "Only pending proposals can be approved"

    def fail_local_approve(*_args):
        raise OSError("LOCAL_DISK_FULL at skill store")

    monkeypatch.setattr(learning, "approve", fail_local_approve)
    output, error = asyncio.run(execute_learning_command(
        learning,
        reviewer,
        memory,
        skills,
        ["approve", "anything"],
        session_id="session-a",
        messages=[],
    ))
    assert output == ""
    assert error == "LOCAL_DISK_FULL at skill store"


def test_learning_provider_cancellation_propagates(tmp_path):
    class CancelledLearningLLM:
        async def chat(self, _messages):
            raise asyncio.CancelledError

    learning = LearningStore(tmp_path / "learning.db")
    memory = MemoryStore(tmp_path / "memory.db")
    skills = SkillStore(tmp_path / "skills")
    reviewer = LearningReviewer(CancelledLearningLLM(), learning, memory, skills)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_learning_command(
            learning,
            reviewer,
            memory,
            skills,
            ["summarize"],
            session_id="session-a",
            messages=[{"role": "user", "content": "summarize this"}],
        ))
