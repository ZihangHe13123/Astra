"""Shared parsing for /memory across CLI frontends."""

from typing import TYPE_CHECKING

from agent.runtime.memory import MemoryStore
from agent.runtime.memory_records import MemoryRecord

if TYPE_CHECKING:
    from agent.runtime.memory_retainer import MemoryRetainer
    from agent.runtime.memory_router import MemoryRouter


MEMORY_USAGE = (
    "Usage:\n"
    "  /memory\n"
    "  /memory review [query]   # discuss evidence and proposed corrections\n"
    "  /memory remember <text>\n"
    "  /memory remember-user <text>\n"
    "  /memory working <temporary_constraints|open_questions|assumptions|turn_notes> <text>\n"
    "  /memory inspect [query|id]\n"
    "  /memory timeline <id>\n"
    "  /memory validate-core\n"
    "  /memory why\n"
    "  /memory correct <id> <replacement text>\n"
    "  /memory forget <id>\n"
    "  /memory clear-working"
)


def _format_record(record: MemoryRecord) -> str:
    source = record.source_session_id or "(unknown)"
    tags = ", ".join(record.tags) if record.tags else "(none)"
    maturity = str(record.metadata.get("maturity", "legacy"))
    evidence = int(record.metadata.get("evidence_count", 1) or 1)
    last_seen = str(record.metadata.get("last_seen", record.last_confirmed_at))
    conflict = str(record.metadata.get("conflict_state", "none"))
    entity_key = str(record.metadata.get("entity_key", "")) or "(none)"
    return (
        f"#{record.record_id} [{record.status}/{record.kind}]\n"
        f"  content: {record.content}\n"
        f"  confidence: {record.confidence:.2f}; salience: {record.salience:.2f}\n"
        f"  lifecycle: maturity={maturity}; evidence={evidence}; conflict={conflict}\n"
        f"  last_seen: {last_seen}; entity_key: {entity_key}\n"
        f"  source: session={source}; message={record.source_message_id or '(unknown)'}\n"
        f"  created: {record.created_at}; valid_until: {record.valid_until or '(open)'}\n"
        f"  supersedes: {record.supersedes_id or '(none)'}; tags: {tags}"
    )


def execute_memory_command(
    store: MemoryStore,
    args: list[str],
    *,
    session_id: str,
    router: "MemoryRouter | None" = None,
    retainer: "MemoryRetainer | None" = None,
) -> tuple[str, str]:
    action = args[0].lower() if args else "status"
    try:
        if action in {"status", "list"}:
            return store.status(session_id), ""
        if action == "inspect":
            query = " ".join(args[1:]).strip()
            if query and " " not in query:
                record = store.resolve_record(query)
                if record is not None:
                    return _format_record(record), ""
            records = store.recall_records(query, limit=20, include_core=True)
            if not records:
                return "No active structured memory records matched.", ""
            return "Structured memory records:\n" + "\n".join(_format_record(item) for item in records), ""
        if action == "timeline":
            if len(args) != 2:
                return "", MEMORY_USAGE
            return store.memory_timeline(args[1]), ""
        if action == "why":
            if router is None:
                return "Memory routing is unavailable in this runtime.", ""
            trace = router.last_trace(session_id)
            retention = retainer.last_outcome(session_id) if retainer is not None else None
            if trace is None and retention is None:
                return "No memory routing decision has been recorded for this session yet.", ""
            blocks = []
            if trace is not None:
                blocks.append(trace.format())
            if retention is not None:
                blocks.append(retention.format())
            return "\n\n".join(blocks), ""
        if action in {"import-core", "validate-core"}:
            result = store.import_core_markdown()
            return (
                f"Validated Core Markdown hard limits; total={result['total']} entries.",
                "",
            )
        if action in {"remember", "remember-user"}:
            content = " ".join(args[1:]).strip()
            if not content:
                return "", MEMORY_USAGE
            scope = "user" if action == "remember-user" else "memory"
            item = store.add_core(scope, content)
            label = "USER.md" if scope == "user" else "MEMORY.md"
            return f"Stored core {label} memory #{item['id']}: {item['content']}", ""
        if action == "forget":
            if len(args) < 2:
                return "", MEMORY_USAGE
            memory_id = args[1]
            core = store.resolve_core(memory_id)
            record = store.resolve_record(memory_id, active_only=True)
            if (
                core is not None
                and record is not None
                and core.get("record_id") != record.record_id
            ):
                return "", f"Memory id #{memory_id} matches both core and structured memory; use a longer id."
            if core is not None and store.remove_core(core["id"]):
                return f"Removed core memory #{core['id']}.", ""
            if record is not None:
                store.forget_record(record.record_id)
                return f"Forgot structured memory #{record.record_id}.", ""
            return "", f"Active memory #{memory_id} not found."
        if action == "correct":
            if len(args) < 3:
                return "", MEMORY_USAGE
            old = store.resolve_record(args[1], active_only=True)
            if old is None:
                return "", f"Active structured memory #{args[1]} not found."
            replacement = store.supersede_record(
                old.record_id,
                content=" ".join(args[2:]),
                source_session_id=session_id,
                confidence=1.0,
                metadata={"retained_by": "explicit-user-correction"},
            )
            return (
                f"Corrected structured memory #{old.record_id}; replacement is "
                f"#{replacement.record_id}: {replacement.content}",
                "",
            )
        if action == "working":
            if len(args) < 3:
                return "", MEMORY_USAGE
            data = store.update_working(session_id, args[1], " ".join(args[2:]))
            return f"Updated working memory: {args[1]}={data[args[1]]}", ""
        if action == "clear-working":
            store.clear_working(session_id)
            return f"Cleared working memory for session {session_id}.", ""
        return "", MEMORY_USAGE
    except (OSError, RuntimeError, ValueError) as exc:
        return "", str(exc)
