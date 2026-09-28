"""User-triggered skill curation; the old candidate queue is retired."""

import asyncio
import json

from agent.runtime.async_io import durable_io
from agent.runtime.learning import LearningStore
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skill_provenance import automatic_names
from agent.runtime.skills import SkillStore

LEARN_USAGE = """Usage:
  /learn                       # direct-learning status
  /learn review [skill-name]   # discuss improvements, then choose edits and verification
  /learn migrate               # import old automatic summaries; retain observations as history
  /learn history [run-id]       # changes and reasons
  /learn undo <run-id>          # restore a change if files are unchanged
  /learn mode <off|review>      # disable/enable direct skill saving
"""

RETIRED_ACTIONS = {
    "legacy", "pending", "list", "show", "reject", "rollback", "summarize", "remember",
    "repair", "repair-all", "repair_all", "approve", "approve-all", "approve_all",
}


async def execute_learning_command(store: LearningStore, skills: SkillStore, args: list[str]) -> tuple[str, str]:
    action = args[0].lower() if args else "status"
    try:
        learned = LearnedSkills(skills)
        if action == "status":
            state = await durable_io(learned._state)
            migrated = state.get("legacy_migrations", {})
            pending = await durable_io(store.list, "pending", 10_000)
            return (f"Direct skill learning: {store.mode()}\n"
                    f"Learned skills: {len(automatic_names(state))}\n"
                    "Review scope: automatic summaries only; user-added and built-in skills are excluded.\n"
                    "Quality review: conversational and user-triggered (/learn review); proposals first, no background review.\n"
                    f"Last review: {json.dumps(state['last_review'], ensure_ascii=False)}\n"
                    f"Legacy migration: {sum(item.get('destination') == 'auto' for item in migrated.values())} imported, "
                    f"{sum(item.get('destination') == 'history' for item in migrated.values())} history-only, "
                    f"{sum(row['id'] not in migrated for row in pending)} not yet migrated (/learn migrate). "
                    "Old candidates stay searchable as history through session_search (source_type=learning).", "")
        if action == "migrate" and len(args) == 1:
            from agent.runtime.skill_migration import format_migration, migrate_legacy

            result = await durable_io(migrate_legacy, store, learned)
            return format_migration(result), ""
        if action == "review" and len(args) == 1:
            return "", "Review now starts a conversation. Use /learn review in the active chat; no changes applied."
        if action == "history" and len(args) <= 2:
            records = await durable_io(learned.history, args[1] if len(args) == 2 else "")
            public = [{key: item[key] for key in ("id", "time", "kind", "status", "actions", "source")}
                      for item in records]
            return json.dumps(public, ensure_ascii=False, indent=2) if public else "No skill learning changes yet.", ""
        if action == "undo" and len(args) == 2:
            result = await durable_io(learned.undo, args[1])
            return f"Restored changes from {args[1]}. Record: {result['id']}", ""
        if action == "mode" and len(args) == 2:
            return f"Direct skill learning: {store.set_mode(args[1])}. Manual /learn review remains available.", ""
        if action in RETIRED_ACTIONS:
            return "", ("The old candidate queue is retired. /learn review checks learned skills, /learn migrate "
                        "imports candidates not yet migrated, and session_search (source_type=learning) finds them as history.")
        return "", LEARN_USAGE
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return "", str(exc)
