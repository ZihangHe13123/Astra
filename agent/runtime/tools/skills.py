"""Agent-facing tools for local procedural skills."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..skills import SkillStore
from ..skill_learning import LearnedSkills
from ..learning_evidence import evidence_messages
from .registry import ToolDef, ToolRegistry, approval_justification_schema

if TYPE_CHECKING:
    from ..learning_lifecycle import LearningLifecycle


def register_skill_tools(
    registry: ToolRegistry, store: SkillStore, *,
    learning_getter: Callable[[], LearningLifecycle] | None = None,
    session_id: Callable[[], str] = lambda: "default",
    messages: Callable[[], list[dict]] = lambda: [],
) -> None:
    def _list() -> str:
        return json.dumps({"skills": store.list()}, ensure_ascii=False, indent=2)

    def _view(name: str, file_path: str = "SKILL.md") -> str:
        return store.view(name, file_path)

    def _manage(
        action: str,
        name: str,
        content: str = "",
        file_path: str = "SKILL.md",
        old_string: str = "",
        new_string: str = "",
        category: str = "",
        origin: str = "auto",
    ) -> str:
        from ..learning import LearningStore
        from ..skill_provenance import automatic_names, read_learning_state

        name = store._validate_name(name)
        file_path = Path(file_path.replace("\\", "/")).as_posix()
        if origin == "user":
            # The model can install/edit a skill at the user's request without
            # turning supplied content into an automatic learning target.
            existing = next((item for item in store.list() if item["name"] == name), None)
            if (name in automatic_names(read_learning_state(store.root))
                    or (existing and existing["origin"] in {"auto", "builtin"})):
                raise ValueError("Skill origins are fixed; do not reclassify an automatic or built-in skill")
            if action == "create":
                result = store.create(name, content, category="user")
            elif action == "patch":
                result = store.patch(name, old_string, new_string, file_path)
            elif action == "write_file":
                result = store.write_file(name, file_path, content)
            else:
                raise ValueError("Unknown skill action")
            return json.dumps({**result, "origin": "user", "notice": "User-added skill; excluded from /learn review."}, ensure_ascii=False)
        if origin != "auto":
            raise ValueError("Skill origin must be auto or user")
        mode = (learning_getter().store.mode() if learning_getter is not None
                else LearningStore(store.root.parent / "learning.db").mode())
        if mode == "off":
            raise ValueError("Learning is off; use /learn mode review to enable saving skills")
        sources = [{"role": item["role"], "excerpt": item["content"][-600:]}
                   for item in evidence_messages(messages())
                   if isinstance(item.get("content"), str)][-3:]
        result = LearnedSkills(store).manage(action, name, content=content, file_path=file_path,
                                             old_string=old_string, new_string=new_string,
                                             source={"session_id": session_id(), "excerpts": sources})
        return json.dumps(result, ensure_ascii=False)

    def _create_project_verifier(name: str = "project-verifier") -> str:
        """Create a reusable verifier skill from stable project markers."""
        root = Path.cwd()
        commands: list[str] = []
        if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
            commands.append("python -m pytest -q")
        if (root / "package.json").is_file():
            commands.append("npm test")
        if (root / "Cargo.toml").is_file():
            commands.append("cargo test")
        if (root / "go.mod").is_file():
            commands.append("go test ./...")
        listed = commands or ["Inspect the project manifest and run its narrowest relevant check."]
        content = "---\nname: " + name + "\ndescription: Execute this project's coding verification contract and report evidence.\n---\n\n# Project verifier\n\nRun the narrowest relevant verification command after a code change. Do not claim success without its actual exit status and output.\n\n## Default checks\n\n" + "\n".join(f"- `{command}`" for command in listed) + "\n\n## Report\n\nFor each command, report PASS or FAIL, exact command, exit status, concise output, and uncovered risk. Stop processes started only for verification.\n"
        return _manage("create", name, content, category="coding")


    def _workflow_design_approval(summary: str) -> str:
        summary = summary.strip()[:500]
        return json.dumps(
            {"approved": True, "summary": summary},
            ensure_ascii=False,
            indent=2,
        )

    def _design_approval_request(args: dict) -> dict:
        summary = str(args.get("summary") or "").strip()
        return {
            "kind": "workflow_design",
            "reason": "Approve this Full workflow design",
            "detail": summary[:500],
            "scope": "workflow-design:current",
            "approval_question": "是否批准这份工作流设计？",
        }

    def _grant_design_approval(
        args: dict,
        request: dict,
        decision: str,
    ):
        # Design approval is intentionally one-revision-only. Even when the
        # frontend offers "session", never create a durable permission grant.
        del args, request, decision
        return None

    registry.register(ToolDef(
        name="skills_list",
        description="List local procedural skills and their descriptions. Use before creating a new skill.",
        parameters={"type": "object", "properties": {}},
        fn=_list,
        risk="read",
        approval="never",
        idempotent=True,
        group="skills",
        # The same list is already in the prompt as <available-skills>; command
        # workflows and delegates keep it through their explicit allowlists.
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="project_verifier_init",
        description="Create a reusable project-specific verification skill from detected build/test markers. It never runs commands or installs dependencies.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "default": "project-verifier"}},
            "additionalProperties": False,
        },
        fn=_create_project_verifier,
        risk="write",
        approval="on_risk",
        max_calls_per_turn=1,
        group="skills",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="skill_view",
        description="Read SKILL.md or one supporting file from a local skill. Always read a target before patching it.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "file_path": {"type": "string", "default": "SKILL.md"},
            },
            "required": ["name"],
        },
        fn=_view,
        risk="read",
        approval="never",
        idempotent=True,
        group="skills",
    ))
    registry.register(ToolDef(
        name="workflow_design_approval",
        description=(
            "When the coding skill selects Full, present one complete design with scope, "
            "risks, alternatives, and verification, then call this tool once. The frontend "
            "pauses for one decision; do not ask for approval keywords or repeat approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise description of the exact design being approved.",
                    "maxLength": 500,
                },
                **approval_justification_schema(),
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        fn=_workflow_design_approval,
        risk="read",
        approval="never",
        permission_check=_design_approval_request,
        permission_grant=_grant_design_approval,
        approval_justification=True,
        idempotent=False,
        max_calls_per_turn=2,
        group="skills",
    ))
    registry.register(ToolDef(
        name="skill_manage",
        description=(
            "Create or conservatively patch a local procedural skill, or write a supporting file. "
            "Store reusable how-to knowledge, not user facts, secrets, transient errors, or one-off narratives."
            "Choose origin=auto only for your own reusable summaries; these save to learned/ and may be reviewed. "
            "When the user supplies a skill or asks you to add/install/edit one, choose origin=user; "
            "these user-owned skills are excluded from review. Never relabel user content as automatic learning. "
            "First inspect the catalog and read an existing skill before updating it. Include when to use it, "
            "steps, source context, and limitations; distinguish observations from verified facts. "
            "Automatic learning cannot change manual/pinned/user-edited skills. No learning quota or separate trial is required. "
            "Only the user can request a library-wide review with /learn review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "patch", "write_file"]},
                "name": {"type": "string"},
                "content": {"type": "string"},
                "file_path": {"type": "string", "default": "SKILL.md"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "category": {
                    "type": "string",
                    "description": "Legacy hint; auto skills use learned/, user additions use user/.",
                },
                "origin": {
                    "type": "string", "enum": ["auto", "user"],
                    "description": "auto: your own task summary; user: content supplied or explicitly added/installed by the user, including when you do it for them.",
                },
            },
            "required": ["action", "name", "origin"],
        },
        fn=_manage,
        risk="write",
        approval="on_risk",
        max_calls_per_turn=8,
        group="skills",
    ))
