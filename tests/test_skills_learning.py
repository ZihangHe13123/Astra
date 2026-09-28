import asyncio
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from agent.runtime.learning import LearningStore
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


def test_learning_mode_is_persistent(tmp_path):
    path = tmp_path / "learning.db"
    store = LearningStore(path)
    assert store.mode() == "review"
    assert store.set_mode("off") == "off"
    assert LearningStore(path).mode() == "off"


