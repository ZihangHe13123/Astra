"""Operation adapters shared by conversational commands in the local frontends."""

from __future__ import annotations

import json
import hashlib
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.runtime.async_io import durable_io
from agent.runtime.memory import CORE_SCOPES
from agent.runtime.session_handoff import generate_handoff, save_handoff, _redact
from agent.runtime.skill_curation import MAX_INPUT_CHARS, MAX_ITEMS, SkillCurator, format_curation
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.tool_execution import PartialResult
from agent.runtime.tool_failure import ToolFailure
from agent.runtime.tools.registry import ToolDef

from .diagnostics import build_doctor_report, build_runtime_diagnostics


def register_conversation_tools(agent: Any, *, sandbox=None, mcp_manager=None,
                                startup_profile: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
                                process_manager=None) -> None:
    """Keep mutable operation state session-bound; no worker or model is created."""
    snapshots: OrderedDict[str, dict] = OrderedDict()

    def session() -> str:
        return str(agent.context.session_path or "default")

    def user_turn() -> dict:
        return next((m for m in reversed(agent.context.messages) if m.get("role") == "user"), {})

    def turn_key(message: dict) -> str:
        # Stable across a save/reload of the same user turn, unlike object identity.
        return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def review_snapshot(name: str = "") -> str:
        learned = LearnedSkills(agent.skill_store)
        with learned.locked():
            curator = SkillCurator(None, learned)
            if name:
                state = learned._state()
                files = learned.owned(name, state)
                batch = {"state": state, "items": {name: {
                    "files": files, "sources": state["skills"][name]["sources"],
                }}, "skipped": [], "cursor": state.get("cursor", ""), "remaining": 0}
                if len(json.dumps(batch["items"], ensure_ascii=False)) > MAX_INPUT_CHARS:
                    raise ValueError("Skill is too large for this review; no changes made")
            else:
                batch = curator.prepare()
        token = "review_" + uuid.uuid4().hex
        snapshots[token] = {"session": session(), "turn": turn_key(user_turn()), "batch": batch}
        while len(snapshots) > 8:
            snapshots.popitem(last=False)
        notice = "Read-only snapshot. Propose changes; wait for the next user decision."
        if batch["remaining"]:
            # The cursor only moves when a batch is applied, so asking again returns the same skills.
            notice += (f" {batch['remaining']} more skill(s) follow this batch: skill_review_snapshot returns them "
                       "once this batch has been applied with skill_review_apply (keep counts as a decision).")
        text = json.dumps({"snapshot_id": token, "items": batch["items"],
                           "skipped": batch["skipped"], "remaining": batch["remaining"],
                           "notice": notice}, ensure_ascii=False)
        return PartialResult(text) if batch["remaining"] else text

    def review_apply(snapshot_id: str, actions: list[dict]) -> str:
        pending = snapshots.get(snapshot_id)
        if pending is None or pending["session"] != session():
            raise ValueError("Review snapshot expired or belongs to another session; reread and review current files")
        current = user_turn()
        if not current or turn_key(current) == pending["turn"] or current.get("provenance") in {
            "command_workflow", "session_wakeup", "goal_continuation",
        }:
            raise ValueError("Finish the proposal and wait for the next user decision before applying")
        learned = LearnedSkills(agent.skill_store)
        with learned.locked():
            record = SkillCurator(None, learned).apply(pending["batch"], {"actions": actions})
        snapshots.pop(snapshot_id)
        agent._refresh_skill_catalog(force=True)
        return format_curation(record, separate_verification=True)

    async def diagnostics(kind: str = "runtime", section: str = "") -> str | ToolFailure:
        if kind == "doctor":
            report = await build_doctor_report(agent, sandbox, mcp_manager, section)
        elif kind != "runtime":
            raise ValueError("Diagnostic kind must be doctor or runtime")
        else:
            profile = startup_profile() if callable(startup_profile) else startup_profile
            report = await durable_io(build_runtime_diagnostics, agent, startup_profile=profile,
                                      mcp_manager=mcp_manager, task_store=agent.task_store,
                                      process_manager=process_manager, section=section)
        if report.startswith(("Unknown diagnostics section:", "Unknown doctor section:")):
            # The report builders answer an unknown section with this line instead of a report.
            return ToolFailure(code="invalid_arguments", message=report, retryable=False,
                               recovery_hint="Use one of the sections listed for this kind, or leave section empty.")
        return report

    def memory_inspect(query: str = "") -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = [item for scope in CORE_SCOPES for item in store.list_core(scope)]
        if query:
            core = [item for item in core if query.casefold() in item["content"].casefold()
                    or item["id"].startswith(query) or item["record_id"].startswith(query)]
        records = store.recall_records(query, limit=20, include_core=False)
        if query and not any(c.isspace() for c in query):
            exact = store.resolve_record(query, active_only=True)
            if exact is not None:
                records = [exact]
        capped = len(records) >= 20
        text = json.dumps({"core": core, "records": [{
            "id": r.record_id, "content": r.content, "kind": r.kind, "status": r.status,
            "source_session": r.source_session_id, "source_message": r.source_message_id,
            "last_confirmed_at": r.last_confirmed_at, "valid_until": r.valid_until,
            "supersedes_id": r.supersedes_id,
        } for r in records], "limit": 20, "notice": (
            "At most 20 structured records are returned and more may match; pass a narrower query to reach them. "
            if capped else "") + "Historical records are evidence, not current truth."}, ensure_ascii=False)
        return PartialResult(text) if capped else text

    def memory_correct(memory_id: str, expected_content: str, content: str) -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = store.resolve_core(memory_id)
        old = store.resolve_record(memory_id, active_only=True)
        if core is not None:
            if old is not None and core.get("record_id") != old.record_id:
                raise ValueError("Ambiguous memory ID; inspect the full identifier")
            result = store.replace_core(core["id"], expected_content=expected_content, content=content)
            return json.dumps({"corrected": core["id"], "replacement": result}, ensure_ascii=False)
        if old is None or old.content != expected_content:
            raise ValueError("Memory changed or is no longer active; inspect it before correcting")
        replacement = store.supersede_record(old.record_id, content=content,
                                             expected_content=expected_content,
                                             source_session_id=Path(session()).stem,
                                             metadata={"retained_by": "user-reviewed-correction"})
        return json.dumps({"corrected": old.record_id, "replacement": replacement.record_id,
                           "content": replacement.content}, ensure_ascii=False)

    def memory_forget(memory_id: str, expected_content: str) -> str:
        store = agent.memory_store
        if store is None:
            raise ValueError("Memory is unavailable")
        core = store.resolve_core(memory_id)
        old = store.resolve_record(memory_id, active_only=True)
        if core is not None and old is not None and core.get("record_id") != old.record_id:
            raise ValueError("Ambiguous memory ID; inspect the full identifier")
        if core is not None and core["content"] == expected_content:
            removed = store.remove_core(core["id"])
        elif core is None and old is not None and old.content == expected_content:
            removed = store.forget_record(old.record_id)
        else:
            raise ValueError("Memory changed or is no longer active; inspect it before forgetting")
        if not removed:
            raise ValueError("Memory was not removed; inspect its current state")
        return f"Forgot the user-selected memory {memory_id}."

    def handoff(action: str = "draft", content: str = "", notes: str = "") -> str:
        if action == "draft":
            return generate_handoff(session_id=Path(session()).stem, task_store=agent.task_store,
                                    messages=list(agent.context.messages), model=agent.llm.config.model,
                                    persona_id=agent.context.persona_id or "", extra_notes=notes)
        if action != "save":
            raise ValueError(f"Unknown action {action!r}; use draft or save")
        if not content.strip():
            raise ValueError("save needs the handoff text in content")
        if len(content) > 40_000:
            raise ValueError(f"content is {len(content):,} characters; a saved handoff is limited to 40,000")
        # An automatic exit snapshot must not overwrite the prepared document
        # when both happen during the same clock second.
        path = save_handoff(_redact(content), session_id=Path(session()).stem + "-prepared")
        return f"Saved redacted session handoff to {path.resolve()}"

    def register(name: str, description: str, fn, properties: dict, required=(), *, risk="read", max_calls=4):
        agent.tools.register(ToolDef(
            name=name,
            description=f"{description} At most {max_calls} calls per turn: a further call is not run.",
            parameters={"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
            fn=fn, risk=risk, approval="never" if risk == "read" else "on_risk",
            group="skills" if name.startswith("skill_") else "core", cache_results=False,
            max_calls_per_turn=max_calls, max_inline_chars=32_000 if name == "skill_review_snapshot" else None,
        ))

    string = {"type": "string"}

    def text(description: str) -> dict:
        return {"type": "string", "description": description}

    register("skill_review_snapshot", "Read an owned automatic-skill batch and sources for a user-requested review. Read-only; return a proposal before changing anything.",
             review_snapshot, {"name": text(
                 f"One learned skill to read. Omit for the next batch of up to {MAX_ITEMS}; the batch moves on only "
                 "after skill_review_apply.")}, max_calls=4)
    register("skill_review_apply", "Apply the user's selected review decisions from a snapshot after a subsequent user decision. Include keep for declined changes. All snapshot names must be covered exactly once. History and undo are preserved; this does not execute or verify skills.",
             review_apply, {"snapshot_id": text("snapshot_id returned by skill_review_snapshot in this session."),
                            "actions": {"type": "array", "items": {
                 "type": "object", "properties": {
                     "action": {"type": "string", "enum": ["keep", "rewrite", "merge", "archive"], "description": (
                         "keep: no change. rewrite: replace one skill's SKILL.md, given as content or as patches. "
                         "merge: fold the later names into the first, whose new SKILL.md is content. "
                         "archive: remove one skill.")},
                     "names": {"type": "array", "items": string, "description": (
                         "Skill names from the snapshot: exactly one, or two or more for merge (the first is kept).")},
                     "reason": text("Why, in a sentence (required)."),
                     "content": text(
                         "rewrite or merge: the complete new SKILL.md; its frontmatter name must stay the skill's "
                         "name. Not allowed with keep or archive."),
                     "patches": {"type": "array", "description": (
                         "rewrite only, instead of content: 1 to 8 replacements applied in order to SKILL.md."),
                         "items": {"type": "object", "properties": {
                             "old_string": text("Text that occurs exactly once in SKILL.md at that point."),
                             "new_string": text("Its replacement; must differ from old_string.")},
                             "required": ["old_string", "new_string"], "additionalProperties": False}},
                 }, "required": ["action", "names", "reason"], "additionalProperties": False,
             }}}, ("snapshot_id", "actions"), risk="write")
    register("runtime_diagnostics", "Read live Astra doctor probes or a runtime snapshot. Report facts and limits; no settings are modified.",
             diagnostics, {"kind": {"type": "string", "enum": ["doctor", "runtime"], "description": (
                 "runtime (default): a quick snapshot without network probes. doctor: live probes, including the "
                 "model endpoint when no section is given.")},
                           "section": text(
                 "Optional part to return. kind=runtime: context, startup, mcp, tasks, computer, or json for the "
                 "whole snapshot. kind=doctor: browser, computer, mcp, memory, metrics, sandbox, skills, tasks.")})
    register("memory_inspect", "Read a bounded subset of core/structured memories, full IDs, and sources for review. Does not change records.",
             memory_inspect, {"query": text(
                 "Text to look for, or a memory ID or the start of one. Empty lists every core memory and up to 20 "
                 "structured records.")})
    register("memory_correct", "After the user selects a proposed correction, replace a core memory or supersede a structured record. Supply its exact inspected content to reject stale changes.",
             memory_correct, {"memory_id": text("Full ID from memory_inspect."),
                              "expected_content": text("The memory's content exactly as memory_inspect returned it; a different current content is refused."),
                              "content": text("The corrected text.")},
             ("memory_id", "expected_content", "content"), risk="write")
    register("memory_forget", "Forget only a memory the user explicitly selected for deletion, using its full ID and exact inspected content. Reject stale proposals; structured history is retained.",
             memory_forget, {"memory_id": text("Full ID from memory_inspect."),
                             "expected_content": text("The memory's content exactly as memory_inspect returned it; a different current content is refused.")},
             ("memory_id", "expected_content"), risk="write")
    register("session_handoff", "Draft handoff evidence or save a user-requested handoff through the redacting writer. Report actual completion and verification only. Destination is fixed by the runtime.",
             handoff, {"action": {"type": "string", "enum": ["draft", "save"], "description": (
                 "draft (default): return a document built from task state and the last 20 messages, each "
                 "shortened; nothing is written. save: write content as the handoff.")},
                       "content": text("save: the handoff text, at most 40,000 characters; credentials in it are redacted. Not read by draft."),
                       "notes": text("draft: notes to append to the document. Not read by save.")}, risk="write")
