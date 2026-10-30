from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.runtime.skills import SkillStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".astra" / "skills"
FOUNDATION_SKILLS = {
    "brainstorming",
    "systematic-debugging",
    "test-driven-development",
    "verification-before-completion",
}
PLANNING_SKILLS = {
    "writing-plans",
    "executing-plans",
    "using-git-worktrees",
    "finishing-a-development-branch",
}
DELEGATION_SKILLS = {
    "requesting-code-review",
    "receiving-code-review",
    "dispatching-parallel-agents",
    "subagent-driven-development",
    "team-tdd-development",
}
EXPECTED_SKILLS = FOUNDATION_SKILLS | PLANNING_SKILLS | DELEGATION_SKILLS
ASTRA_TOOLS = {
    "read_file",
    "execute_shell",
    "apply_patch",
    "plan_update",
    "ask_user_question",
    "skills_list",
    "skill_view",
    "delegate_task",
    "delegate_send",
    "delegate_wait",
    "delegate_list",
    "delegate_read",
    "delegate_cancel",
}
# The Agent Team tools were merged into delegate_*; a skill that names one sends the model to a tool it lacks.
RETIRED_TOOLS = ("team(", "team_spawn", "team_restart", "team_send", "team_wait", "team_inbox", "team_task",
                 "delegate_poll", "keep_alive")
FORBIDDEN_TEXT = {
    "request_user_input",
    "functions.",
    "/Users/",
    "C:\\Users\\",
    "superpowers:",
}


def skill_path(name: str) -> Path:
    return SKILLS_ROOT / "development" / name / "SKILL.md"


def listed_development_skills() -> dict[str, dict[str, object]]:
    return {
        str(item["name"]): item
        for item in SkillStore(SKILLS_ROOT).list()
        if item["category"] == "development"
    }


@pytest.mark.parametrize("name", sorted(FOUNDATION_SKILLS))
def test_foundation_development_skills_are_discoverable_and_viewable(name: str):
    listed = listed_development_skills()
    assert name in listed
    assert listed[name]["files"] == 1
    content = SkillStore(SKILLS_ROOT).view(name)
    assert content.startswith(f"---\nname: {name}\n")
    assert len(content) < 20_000


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_canonical_development_skill_files_are_visible_to_git(name: str):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", str(skill_path(name))],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1


def test_unlisted_development_skill_remains_ignored():
    unlisted = SKILLS_ROOT / "development" / "unlisted" / "SKILL.md"
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", str(unlisted)],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("name", sorted(PLANNING_SKILLS))
def test_planning_development_skills_are_discoverable_and_viewable(name: str):
    listed = listed_development_skills()
    assert name in listed
    assert listed[name]["files"] == 1
    assert SkillStore(SKILLS_ROOT).view(name).startswith(f"---\nname: {name}\n")


def test_planning_skills_form_a_resolvable_local_workflow():
    store = SkillStore(SKILLS_ROOT)
    assert "writing-plans" in store.view("brainstorming")
    assert "executing-plans" in store.view("writing-plans")
    assert "using-git-worktrees" in store.view("executing-plans")
    assert "verification-before-completion" in store.view("finishing-a-development-branch")


def test_multi_step_execution_routes_to_team_tdd():
    store = SkillStore(SKILLS_ROOT)
    executing = store.view("executing-plans")
    team_tdd = store.view("team-tdd-development")

    assert "`team-tdd-development`" in executing
    assert "multi-step" in executing.lower()
    for dependency in {
        "executing-plans",
        "using-git-worktrees",
        "test-driven-development",
        "verification-before-completion",
        "finishing-a-development-branch",
    }:
        assert f"`{dependency}`" in team_tdd


def test_team_tdd_sizes_each_assignment_by_time_not_turns():
    content = SkillStore(SKILLS_ROOT).view("team-tdd-development")

    assert "delegate_task" in content
    assert "timeout" in content
    assert "1800" in content and "14400" in content
    assert "no turn limit" in content
    assert "three worker-reviewer rounds" in content
    # Nothing in the workflow asks for a turn count the runtime no longer has.
    for retired in ("max_turns", "turns_remaining", "episode_estimate"):
        assert retired not in content, retired


def test_team_tdd_reuses_its_two_subagents_and_checks_them_before_every_assignment():
    content = SkillStore(SKILLS_ROOT).view("team-tdd-development")

    assert "name=worker" in content and "name=reviewer" in content
    assert 'delegate_send(to="worker"' in content and 'delegate_send(to="reviewer"' in content
    assert "delegate_list" in content and "before every" in content.lower()
    assert "neither a `group`" in content


def test_team_tdd_binds_and_verifies_the_lead_workspace_root():
    content = SkillStore(SKILLS_ROOT).view("team-tdd-development")

    assert "workspace_root" in content
    assert "both" in content.lower()
    assert "absolute" in content.lower()
    assert "actual git root" in content.lower()
    assert "do not assign" in content.lower()


def test_team_tdd_does_not_trust_forced_or_readiness_reports_as_review():
    content = SkillStore(SKILLS_ROOT).view("team-tdd-development")

    assert "forced-finalization" in content.lower()
    assert "transcript" in content.lower()
    assert "readiness report" in content.lower()
    assert "cannot satisfy" in content.lower()
    assert "APPROVE" in content
    assert "CHANGES_REQUIRED" in content


def test_team_tdd_stops_every_working_subagent_on_all_exit_paths():
    content = SkillStore(SKILLS_ROOT).view("team-tdd-development")

    assert "finally" in content.lower()
    for path in ("blocker", "timeout", "cancellation", "unsafe recovery"):
        assert path in content.lower(), path
    assert "delegate_cancel" in content and "close=true" in content
    assert "recover=true" in content


@pytest.mark.parametrize("name", sorted(DELEGATION_SKILLS | PLANNING_SKILLS))
def test_skills_name_no_retired_team_tool(name: str):
    content = SkillStore(SKILLS_ROOT).view(name)
    for retired in RETIRED_TOOLS:
        assert retired not in content, (name, retired)


def test_one_shot_subagent_skill_does_not_compete_with_team_default():
    content = SkillStore(SKILLS_ROOT).view("subagent-driven-development")
    frontmatter = SkillStore._frontmatter(content)

    assert "one-shot" in frontmatter["description"].lower()
    assert "explicit" in frontmatter["description"].lower()
    assert "`team-tdd-development`" in content


def test_exact_development_skill_catalog_is_installed():
    assert set(listed_development_skills()) == EXPECTED_SKILLS


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_every_development_skill_is_self_contained_and_portable(name: str):
    path = skill_path(name)
    content = SkillStore(SKILLS_ROOT).view(name)
    assert path.is_file()
    assert list(path.parent.iterdir()) == [path]
    assert all(token not in content for token in FORBIDDEN_TEXT)
    assert "commentary channel" not in content
    assert "final channel" not in content


def test_named_astra_tools_exist_in_runtime_registrations():
    tool_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "agent" / "runtime" / "tools").glob("*.py")
    )
    for tool_name in ASTRA_TOOLS:
        assert f'name="{tool_name}"' in tool_sources


def test_all_local_skill_references_resolve():
    store = SkillStore(SKILLS_ROOT)
    for name in EXPECTED_SKILLS:
        content = store.view(name)
        for referenced in EXPECTED_SKILLS:
            if f"`{referenced}`" in content:
                assert store.view(referenced).startswith("---\n")
