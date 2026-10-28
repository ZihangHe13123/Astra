"""Which tools every request carries, and how the others are found and called (specs/tool-disclosure.md).

By default (``ASTRA_TOOL_DISCLOSURE=deferred``) a conversation is sent a small core set plus ``tool_search`` and
``call_tool``. Every other default tool stays registered and runs as before, but its schema reaches the model
only inside a ``tool_search`` result, and the model calls it through ``call_tool``. The tools array of a
conversation therefore never changes, so the provider's cached prefix survives a tool being needed late.
``full`` sends every default tool, as before, and neither dispatch tool.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolDef, ToolRegistry

DISCLOSURE_ENV = "ASTRA_TOOL_DISCLOSURE"
SEARCH_TOOL = "tool_search"
CALL_TOOL = "call_tool"
DISPATCH_TOOLS = frozenset({SEARCH_TOOL, CALL_TOOL})
# The line a tool_search result (or a refused call_tool) starts with; the conversation's loaded set is
# read back from these lines, so it follows restores, branches and compaction without state of its own.
LOADED_PREFIX = "Loaded tools: "
SEARCH_LIMIT = 20

# Measured on 2026-10-27 (specs/tool-disclosure.md §3): what most conversations use. ``run_code`` and
# ``read_image_tiles`` are filtered by their own rules (Code Mode, the vision-tile preference), so they
# are never deferred either.
CORE_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "apply_patch", "search_text", "search_files", "stat_file",
    "execute_shell", "execute_python", "process_poll", "process_read", "process_cancel", "process_list",
    "ask_user_question", "plan_update", "goal", "memory", "skill_view", "session_search", "activity_search",
    "context_open", "search_web", "web_extract", "fetch_url", "read_image", "current_time", "delegate_task",
    "run_code", "read_image_tiles",
})


def disclosure_mode() -> str:
    return "full" if os.getenv(DISCLOSURE_ENV, "").strip().lower() == "full" else "deferred"


def _prefixed(*prefixes: str, names: Iterable[str] = ()) -> Callable[[str], bool]:
    exact = frozenset(names)
    return lambda name: name in exact or name.startswith(prefixes)


# One catalog line per family, in this order: an id the model passes to tool_search, and what it is for.
FAMILIES: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
    ("browser", "drive the user's browser: open, read, click, fill and upload on web pages", _prefixed("browser_")),
    ("computer", "operate apps and windows on this Mac: look, click, type, take over", _prefixed("computer_")),
    ("git", "repository status, diffs, commits, branches and push", _prefixed("git_")),
    ("subagents", "follow, read and cancel delegated subagents; agent teams, their messages and tasks",
     _prefixed("delegate_", "team")),
    ("documents", "write, edit, check and export Word/PDF documents written in Markdown", _prefixed("doc_")),
    ("canvas", "slide decks and documents shown beside the conversation: make one, look at a slide, export a deck",
     _prefixed(names=("canvas", "canvas_look"))),
    ("schedule", "scheduled jobs and reminders, and wake-ups of this conversation",
     _prefixed(names=("jobs", "schedule_wakeup", "wakeup_status", "cancel_wakeup", "report_wakeup"))),
    ("calendar", "the user's calendar and reminders", _prefixed(names=("calendar",))),
    ("mail", "the user's mailbox", _prefixed(names=("mail",))),
    ("peers", "other Astra conversations and machines: list them, hand them tasks, report on a task",
     _prefixed("peer_")),
    ("skills", "create, edit and review skills; approve a workflow design",
     _prefixed("skill_", names=("workflow_design_approval",))),
    ("memory", "inspect, correct or forget saved memories", _prefixed("memory_", "hindsight_")),
    ("images", "search the web for images; read an image file's metadata",
     _prefixed(names=("search_images", "read_search_images", "inspect_image_metadata"))),
    ("channel", "send a file into the current WeChat or QQ chat", _prefixed("channel_")),
    ("session", "hand this conversation over; inspect the indexed context",
     _prefixed(names=("session_handoff", "context_inspect"))),
    ("system", "restart Astra, runtime diagnostics, load the workspace's Python dependencies",
     _prefixed(names=("request_restart", "runtime_diagnostics", "load_workspace_dependencies"))),
)


def family_of(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp:" + name.split("__")[1]
    for family, _, matches in FAMILIES:
        if matches(name):
            return family
    return "other"


def _families(tools: Iterable[ToolDef]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for tool in tools:
        found.setdefault(family_of(tool.name), []).append(tool.name)
    return {family: sorted(names) for family, names in found.items()}


def catalog(tools: Iterable[ToolDef]) -> tuple[str, list[str]]:
    """The catalog lines tool_search's description carries, and the family ids in that order."""
    found = _families(tools)
    purposes = {family: purpose for family, purpose, _ in FAMILIES}
    order = [family for family, _, _ in FAMILIES if family in found]
    order += sorted(family for family in found if family.startswith("mcp:"))
    order += ["other"] if "other" in found else []
    lines = []
    for family in order:
        if family.startswith("mcp:"):
            purpose = f"tools of the {family[4:]} MCP server"
        else:
            purpose = purposes.get(family) or "other tools: " + ", ".join(found[family])
        lines.append(f"- {family}: {purpose}")
    return "\n".join(lines), order


SEARCH_DESCRIPTION = (
    "Load tools that are not in your tool list. They are grouped by family below; load what the task "
    "needs (a whole family, exact names, or a keyword search) and you get each tool's full definition. "
    "Then call a loaded tool with call_tool. Load before you call; a tool loaded earlier in this "
    "conversation needs no new search unless the conversation was compacted.\nFamilies:\n"
)
CALL_DESCRIPTION = (
    "Call a tool that tool_search loaded: name is the tool's name, arguments its arguments as the "
    "definition describes them. Approvals and results are the same as for any other tool."
)


def tool_schema(tool: ToolDef) -> dict:
    return {"name": tool.name, "description": tool.description, "parameters": tool.parameters}


def loaded_result(tools: list[ToolDef], note: str = "") -> str:
    """A result that hands the model these definitions; its first line marks them as loaded."""
    lines = [LOADED_PREFIX + ", ".join(tool.name for tool in tools)]
    if note:
        lines.append(note)
    lines.append('Call them with call_tool(name="<tool>", arguments={...}). Definitions:')
    lines += [json.dumps(tool_schema(tool), ensure_ascii=False, separators=(",", ":")) for tool in tools]
    return "\n".join(lines)


def loaded_names(text: str) -> set[str]:
    """The tools a tool_search or call_tool result handed over, from its marker line."""
    names: set[str] = set()
    for line in re.findall(rf"^{re.escape(LOADED_PREFIX)}([^\n]*)$", text or "", flags=re.MULTILINE):
        names.update(item.strip() for item in line.split(",") if item.strip())
    return names


def search(deferred: list[ToolDef], *, family: str = "", names: Iterable[str] = (), query: str = "") -> list[ToolDef]:
    """Deferred tools by family, by exact name, or by keywords over their names and descriptions."""
    by_name = {tool.name: tool for tool in deferred}
    chosen: dict[str, ToolDef] = {}
    if family:
        for tool in deferred:
            if family_of(tool.name) == family:
                chosen[tool.name] = tool
    for name in names:
        if name in by_name:
            chosen[name] = by_name[name]
    words = [word for word in re.split(r"[\s,;/]+", (query or "").lower()) if word]
    if words:
        scored = []
        for tool in deferred:
            haystack = f"{tool.name} {family_of(tool.name)} {tool.description}".lower()
            score = sum(3 if word in tool.name.lower() else 1 for word in words if word in haystack)
            if score:
                scored.append((-score, tool.name, tool))
        for _, _, tool in sorted(scored)[:8]:
            chosen.setdefault(tool.name, tool)
    return list(chosen.values())[:SEARCH_LIMIT]


def register_dispatch_tools(registry: ToolRegistry) -> None:
    """tool_search and call_tool; the registry sends them only when disclosure is ``deferred``."""
    from agent.runtime.tool_failure import ToolFailure

    from .registry import ToolDef

    def tool_search(family: str = "", names: list[str] | None = None, query: str = "") -> str | ToolFailure:
        requested = [str(name).strip() for name in (names or []) if str(name).strip()]
        if not (family or requested or (query or "").strip()):
            return ToolFailure(code="invalid_arguments", retryable=True,
                               message="Give a family, names or a query.")
        deferred = registry.deferred_tools()
        found = search(deferred, family=family.strip(), names=requested, query=query)
        unknown = [name for name in requested if name not in {tool.name for tool in deferred}]
        note = ""
        if unknown:
            sent = [name for name in unknown if registry.get(name) is not None and not registry.is_deferred(name)]
            missing = [name for name in unknown if name not in sent]
            parts = []
            if sent:
                parts.append(f"already in your tool list, call directly: {', '.join(sent)}")
            if missing:
                parts.append(f"no such tool to load: {', '.join(missing)}")
            note = "; ".join(parts)
        if not found:
            families = ", ".join(catalog(deferred)[1])
            return ToolFailure(code="not_found", retryable=True,
                               message=(note + ". " if note else "") + f"Nothing matched. Families: {families}.")
        return loaded_result(found, note)

    def call_tool(name: str, arguments: dict | None = None) -> ToolFailure:
        # The tool loop runs the named tool in this call's place; reaching here means no loop did.
        return ToolFailure(code="unavailable", retryable=False,
                           message="call_tool only works inside a conversation's tool loop.")

    registry.register(ToolDef(
        name=SEARCH_TOOL,
        description=SEARCH_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "family": {"type": "string", "description": "A family from the list, to load all of its tools."},
                "names": {"type": "array", "items": {"type": "string"}, "description": "Exact tool names to load."},
                "query": {"type": "string", "description": "Keywords, when you do not know the family or names."},
            },
            "additionalProperties": False,
        },
        fn=tool_search,
        sandboxed=True,
        idempotent=True,
        timeout=10.0,
        # Twenty definitions can run to tens of thousands of characters; they are useless cut short.
        max_inline_chars=80_000,
    ))
    registry.register(ToolDef(
        name=CALL_TOOL,
        description=CALL_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The loaded tool's name."},
                "arguments": {"type": "object", "description": "Its arguments."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        fn=call_tool,
        sandboxed=True,
        timeout=5.0,
    ))
