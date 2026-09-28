"""Model-directed native learning, using normal task tools for verification.

No entry point registers these tools since learning moved to direct skill saving
and manual /learn review (2026-09-13; tests/test_skill_curation.py guards that).
They remain the only way to open candidate trials, so tests/test_learning_lifecycle.py
drives the legacy candidate queue through them. Delete this module together with
the LearningLifecycle trial methods and the legacy /learn candidate commands.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..learning import LearningReviewer
from ..learning_evidence import evidence_messages, is_evidence_tool
from ..learning_lifecycle import LearningLifecycle
from .registry import ToolDef, ToolRegistry


def register_learning_tools(
    registry: ToolRegistry, lifecycle: LearningLifecycle, *, agent_getter: Callable[[], Any],
) -> None:
    def context() -> tuple[str, str, list[dict]]:
        from pathlib import Path

        agent = agent_getter()
        return (Path(agent.context.session_path).stem or "default",
                str(getattr(agent, "_context_index_request_id", "") or ""),
                list(agent.context.messages))

    def normalize(proposal: dict, messages: list[dict], candidate_id: str = "") -> dict:
        sources = evidence_messages(messages)
        prior = lifecycle.store.get(candidate_id) if candidate_id and lifecycle.managed(candidate_id) else None
        result = LearningReviewer._normalize(
            proposal, user_messages=LearningReviewer._message_texts(sources, "user"),
            tool_messages=LearningReviewer._message_texts(sources, "tool"),
            prior=prior,
        )
        if result is None:
            raise ValueError("Invalid candidate: cite an exact original user/tool source for observations; provide reusable skill steps and no credentials")
        return result

    def search(query: str = "", limit: int = 8) -> str:
        return json.dumps({"mode": lifecycle.store.mode(), "candidates": lifecycle.search(query, limit),
                           "notice": "Pending candidates are unverified. Use learning show/trial deliberately; they are not established memory."}, ensure_ascii=False)

    def learning(action: str, candidate_id: str = "", proposal: dict | None = None,
                 contract: dict | None = None, trial_id: str = "", case: str = "",
                 checks: list[dict] | None = None, phase: str = "validation", variant: str = "candidate") -> str:
        session_id, request_id, messages = context()
        if action == "show":
            result = lifecycle.show(candidate_id)
            from ..learning_queue import candidate_readiness
            result["readiness"] = candidate_readiness(lifecycle, candidate_id)
        elif action in {"propose", "revise"}:
            normalized = normalize(proposal or {}, messages, candidate_id if action == "revise" else "")
            if action == "propose":
                result = lifecycle.propose(normalized, session_id, contract=contract, messages=messages, request_id=request_id)
            else:
                result = lifecycle.revise(candidate_id, normalized, contract=contract or {}, session_id=session_id, messages=messages, request_id=request_id)
        elif action == "trial":
            for check in checks or []:
                tool = registry.get(str(check.get("tool") or ""))
                if tool is None or tool.result_persistence == "request_local" or tool.argument_persistence == "request_local":
                    raise ValueError("Use an available native tool with durable, non-secret evidence for a check")
            result = lifecycle.start_trial(candidate_id, session_id=session_id, request_id=request_id,
                                           case=case, checks=checks or [], phase=phase, variant=variant)
        elif action == "finish":
            result = lifecycle.finish_trial(trial_id, session_id=session_id, request_id=request_id)
        elif action == "activate":
            result = lifecycle.activate(candidate_id)
        elif action == "discard":
            result = lifecycle.discard(candidate_id)
        elif action == "rollback":
            result = lifecycle.rollback(candidate_id)
        else:
            raise ValueError("Unknown learning action")
        return json.dumps(result, ensure_ascii=False)

    def observe(name: str, args: dict, result: dict, tool: ToolDef) -> None:
        if not is_evidence_tool(name) or tool.result_persistence == "request_local" or tool.argument_persistence == "request_local":
            return
        session_id, request_id, _ = context()
        lifecycle.observe(session_id, request_id, name, args, result)

    registry.hooks.on_tool_result(observe)
    registry.register(ToolDef(
        name="learning_search", description=(
            "Find scoped learning candidates and previously verified learning before proposing a duplicate. "
            "Use when a relevant task may test or improve an experience; ordinary turns need no learning call. "
            "Pending candidates never enter established memory or the skill catalog automatically. "
            "For short completed UI tasks, report the verified result promptly; optional learning may wait for a relevant later task."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        }, "additionalProperties": False},
        fn=search, risk="read", approval="never", cache_results=False, idempotent=True,
        max_calls_per_turn=3, group="skills", strict_schema=True,
    ))
    registry.register(ToolDef(
        name="learning", description=(
            "Choose what and when to learn: propose/revise a candidate, trial it in a relevant authorized task, "
            "then activate verified learning. First search existing candidates/skills. Specify trigger, benefit, "
            "verification_plan and claim_type (fact/procedure/hypothesis/improvement). No learning quota. "
            "For revise, omitted contract fields keep their previous values; supply only fields you intend to change. "
            "A trial returns isolated versioned instructions: declare exact tool args and observable assertions BEFORE "
            "executing normal tools, then finish; Astra records actual receipts. At most two trials per request. "
            "Do not claim pass from your own prose, retrieval, repeated summaries or tool startup. "
            "Keep new validation inputs separate from training/revision inputs. Failed checks require revision; "
            "inconclusive runs may be retried. Activation does not expand scope or permissions. "
            "Facts must equal a literal verified contains assertion; hypotheses remain candidates. Procedures require "
            "a successful validation; improvement claims additionally need paired old/new checks on two held-out cases. "
            "show inspects proof; discard rejects a pending item; rollback restores a verified applied version if unchanged. "
            "Core Markdown and manual/pinned skill changes retain explicit user maintenance paths."
            " Example proposal: {kind:'skill_create',payload:{name:'form-check',category:'operations',"
            "content:'...'},reason:'verified reusable procedure'}. Skill fields belong inside proposal.payload."
        ),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["show", "propose", "revise", "trial", "finish", "activate", "discard", "rollback"]},
            "candidate_id": {"type": "string", "pattern": "^lr_[0-9a-f]{8}$"},
            "proposal": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["memory", "observation", "skill_create", "skill_patch", "skill_write_file"]},
                "payload": {"type": "object", "description": "Fields depend on kind. skill_create requires name/content, with optional category; memory requires scope/content/evidence; observation requires content/evidence/evidence_role; skill_patch requires name/old_string/new_string; skill_write_file requires name/file_path/content.",
                    "properties": {
                        **{key: {"type": "string"} for key in (
                            "name", "content", "category", "file_path", "old_string", "new_string",
                            "scope", "evidence", "evidence_role")},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    }},
                "reason": {"type": "string", "maxLength": 1000},
            }, "required": ["kind", "payload", "reason"], "additionalProperties": False},
            "contract": {"type": "object", "properties": {
                "claim_type": {"type": "string", "enum": ["fact", "procedure", "hypothesis", "improvement"]},
                **{key: {"type": "string", "maxLength": 1000} for key in ("trigger", "benefit", "verification_plan")},
            }, "additionalProperties": False},
            "trial_id": {"type": "string", "pattern": "^lt_[0-9a-f]{12}$"},
            "case": {"type": "string", "minLength": 3, "maxLength": 300},
            "phase": {"type": "string", "enum": ["training", "validation"]},
            "variant": {"type": "string", "enum": ["candidate", "baseline"]},
            "checks": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                "type": "object", "properties": {
                    "tool": {"type": "string"}, "args": {"type": "object"},
                    "contains": {"type": "string", "minLength": 3, "maxLength": 1000},
                    "json_path": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": ["string", "integer"]}},
                    "equals": {},
                }, "required": ["tool", "args"], "additionalProperties": False,
            }},
        }, "required": ["action"], "additionalProperties": False},
        fn=learning, risk="write", approval="never", cache_results=False,
        max_calls_per_turn=12, group="skills", strict_schema=True,
    ))


__all__ = ["register_learning_tools"]
