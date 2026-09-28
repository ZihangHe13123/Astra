import asyncio
import json
from pathlib import Path

import pytest

from agent.runtime.tools.documents import (
    LEAD_ID,
    DocumentError,
    build_document,
    insert_section,
    parse_document,
    remove_comment,
    replace_section,
)
from agent.runtime.tools.files import FilesystemPolicy, FilesystemRoot, register_file_tools
from agent.runtime.tools.documents import register_document_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def make_registry(workspace: Path, policy: FilesystemPolicy | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(workspace), policy=policy)
    register_document_tools(registry)
    return registry


def call(registry: ToolRegistry, name: str, args: dict) -> dict:
    result = run(registry.execute(name, args))
    if result.get("error"):
        return result
    return json.loads(result["output"])


def create(registry: ToolRegistry, path: str = "plan.md", **extra) -> dict:
    args = {
        "path": path,
        "title": "Plan",
        "lead": "The plan in one line.",
        "sections": [
            {"heading": "Background", "intent": "Why this matters"},
            {"heading": "Method", "intent": "How we do it"},
            {"heading": "Budget", "content": "About $0."},
        ],
        **extra,
    }
    return call(registry, "doc_create", args)


def section(outline: dict, section_id: str) -> dict:
    return next(item for item in outline["sections"] if item["id"] == section_id)


# ---- parsing -------------------------------------------------------------------------------


def test_parse_uses_markers_then_heading_slugs_and_keeps_ids_unique():
    document = parse_document(
        "# Title\n\nLead.\n\n"
        '<!-- astra:section id="intro" -->\n## Intro\n\nText.\n\n'
        "## Results\n\nA.\n\n### Details\n\nB.\n\n## Results\n\nC.\n\n## 数据 采集\n\nD.\n"
    )
    assert document.title == "Title"
    assert [item.id for item in document.sections] == ["intro", "results", "details", "results-2", "数据-采集"]
    assert [item.marked for item in document.sections] == [True, False, False, False, False]
    results = document.section("results")
    details = document.section("details")
    # A level-2 section spans its level-3 sub-section; its own word count stops at the child.
    assert results.start < details.start < details.end == results.end
    assert results.words == 1


def test_fenced_code_is_not_parsed_for_headings_markers_or_comments():
    document = parse_document(
        "# Title\n\n## Code\n\n```md\n## not a heading\n<!-- @astra: not a comment -->\n"
        '<!-- astra:section id="fake" -->\n```\n\nAfter.\n'
    )
    assert [item.id for item in document.sections] == ["code"]
    assert document.comments == []


def test_pending_status_requires_an_untouched_placeholder():
    text = build_document("Plan", "", [{"heading": "Method", "intent": "How we do it"}])
    assert "*Pending: How we do it*" in text
    assert parse_document(text).section("method").status == "pending"
    edited = text.replace("*Pending: How we do it*", "*Pending: How we do it*\n\nA user note.")
    assert parse_document(edited).section("method").status == "written"


def test_section_hash_ignores_trailing_spacing_but_not_content():
    base = parse_document("# T\n\n## A\n\nText.\n\n## B\n\nMore.\n")
    spaced = parse_document("# T\n\n## A\n\nText.\n\n\n\n## B\n\nMore.\n")
    changed = parse_document("# T\n\n## A\n\nText!\n\n## B\n\nMore.\n")
    assert base.section("a").hash == spaced.section("a").hash
    assert base.section("a").hash != changed.section("a").hash


def test_replace_keeps_other_sections_byte_identical_and_crlf():
    text = "# T\r\n\r\n## A\r\n\r\nOld.\r\n\r\n\r\n## B\r\n\r\nKeep  spacing   here.\r\n"
    document = parse_document(text)
    updated = "".join(replace_section(document, "a", "New text."))
    assert "\r\n" in updated and "\n" not in updated.replace("\r\n", "")
    assert "Keep  spacing   here." in updated
    assert parse_document(updated).section("b").hash == document.section("b").hash


def test_content_heading_rules():
    document = parse_document(build_document("T", "", [{"heading": "A", "content": "x"}, {"heading": "B"}]))
    # A repeated heading on the first line is taken as the heading, not duplicated.
    updated = "".join(replace_section(document, "a", "## A renamed\n\nBody"))
    assert "## A renamed" in updated and updated.count("## A") == 1
    with pytest.raises(DocumentError) as same_level:
        replace_section(document, "a", "Body\n\n## Next")
    assert same_level.value.code == "invalid_content"
    with pytest.raises(DocumentError):
        replace_section(document, "a", '<!-- astra:section id="x" -->\n### Sub')
    deeper = "".join(replace_section(document, "a", "Body\n\n### Sub\n\nMore"))
    assert "### Sub" in deeper
    with pytest.raises(DocumentError):
        replace_section(document, LEAD_ID, "## Not allowed in the lead")


def test_insert_positions_and_levels():
    document = parse_document(build_document("T", "", [{"heading": "A", "content": "a"}, {"heading": "B", "content": "b"}]))
    lines, new_id = insert_section(document, "Sub", content="s", level=3, after="a")
    updated = parse_document("".join(lines))
    assert new_id == "sub"
    assert [item.id for item in updated.sections] == ["a", "sub", "b"]
    lines, first = insert_section(updated, "Zero", before="a", intent="Opening")
    ordered = parse_document("".join(lines))
    assert [item.id for item in ordered.sections][0] == first == "zero"
    assert ordered.section("zero").status == "pending"
    with pytest.raises(DocumentError):
        insert_section(ordered, "X", after="a", before="b")
    with pytest.raises(DocumentError):
        insert_section(ordered, "X", section_id="a")


def test_comments_inline_and_whole_line_are_removed_cleanly():
    text = (
        "# T\n\n## A\n\nFirst <!-- @astra: tighten --> sentence.\n\n"
        "<!-- @astra: add a table -->\n\nSecond.\n\n## B\n\n<!-- @astra:\n  multi line\n-->\nEnd.\n"
    )
    document = parse_document(text)
    assert [(item.text, item.section_id) for item in document.comments] == [
        ("tighten", "a"), ("add a table", "a"), ("multi line", "b"),
    ]
    assert document.comments[0].context == "First sentence."
    assert document.comments[1].context == "First sentence."
    assert document.comments[2].context == "## B"
    once = "".join(remove_comment(document, document.comments[0].id))
    assert "First sentence." in once
    twice = parse_document(once)
    cleaned = "".join(remove_comment(twice, twice.comments[0].id))
    assert "add a table" not in cleaned and "\n\n\n" not in cleaned
    third = parse_document(cleaned)
    final = "".join(remove_comment(third, third.comments[0].id))
    assert "multi line" not in final and "## B\n\nEnd.\n" in final
    with pytest.raises(DocumentError):
        remove_comment(parse_document(final), "cmissing")


def test_comment_ids_are_stable_when_other_text_changes():
    before = parse_document("# T\n\n## A\n\nText <!-- @astra: fix --> here.\n")
    after = parse_document("# T\n\nNew lead.\n\n## A\n\nChanged text <!-- @astra: fix --> here.\n")
    assert before.comments[0].id == after.comments[0].id


# ---- tools ----------------------------------------------------------------------------------


def test_create_outline_and_fill_pending_without_hash(tmp_path):
    registry = make_registry(tmp_path)
    created = create(registry)
    assert created["status"] == "created"
    assert [item["status"] for item in created["sections"]] == ["pending", "pending", "written"]
    assert created["pending_sections"] == 2
    assert section(created, "background")["intent"] == "Why this matters"

    written = call(registry, "doc_write_section", {
        "path": "plan.md", "section_id": "background", "content": "Rules fail when the hand turns.",
    })
    assert written["section"]["status"] == "written"
    assert written["pending_sections"] == 1
    text = (tmp_path / "plan.md").read_text(encoding="utf-8")
    assert '<!-- astra:section id="background" -->\n## Background\n\nRules fail when the hand turns.\n' in text

    outline = call(registry, "doc_outline", {"path": "plan.md", "section_id": "background"})
    assert outline["section"]["markdown"] == "## Background\n\nRules fail when the hand turns."
    assert outline["lead"]["words"] == 5


def test_create_refuses_existing_and_non_markdown(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    assert create(registry)["code"] == "doc_exists"
    assert create(registry, path="plan.txt")["code"] == "not_markdown"
    assert call(registry, "doc_outline", {"path": "missing.md"})["code"] == "doc_not_found"


def test_written_sections_need_the_current_hash(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    call(registry, "doc_write_section", {"path": "plan.md", "section_id": "background", "content": "v1"})

    missing = call(registry, "doc_write_section", {"path": "plan.md", "section_id": "background", "content": "v2"})
    assert missing["code"] == "hash_required"
    stale = call(registry, "doc_write_section", {
        "path": "plan.md", "section_id": "background", "content": "v2", "expected_hash": "0" * 16,
    })
    assert stale["code"] == "section_changed"

    outline = call(registry, "doc_outline", {"path": "plan.md"})
    budget_hash = section(outline, "budget")["hash"]
    ok = call(registry, "doc_write_section", {
        "path": "plan.md", "section_id": "background", "content": "v2",
        "heading": "Motivation", "expected_hash": section(outline, "background")["hash"],
    })
    assert ok["section"]["heading"] == "Motivation"
    after = call(registry, "doc_outline", {"path": "plan.md"})
    assert section(after, "budget")["hash"] == budget_hash


def test_user_edits_are_never_silently_overwritten(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    path = tmp_path / "plan.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("*Pending: How we do it*", "My own draft of the method."),
        encoding="utf-8",
    )
    refused = call(registry, "doc_write_section", {"path": "plan.md", "section_id": "method", "content": "Agent text"})
    assert refused["code"] == "hash_required"
    assert "My own draft of the method." in path.read_text(encoding="utf-8")


def test_add_and_remove_sections(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    added = call(registry, "doc_add_section", {
        "path": "plan.md", "heading": "Risks", "intent": "What could slip", "after": "method",
    })
    assert added["sections"] == ["background", "method", "risks", "budget"]
    assert added["section"]["status"] == "pending"

    removed = call(registry, "doc_remove_section", {"path": "plan.md", "section_id": "risks"})
    assert removed["sections"] == ["background", "method", "budget"]

    needs_hash = call(registry, "doc_remove_section", {"path": "plan.md", "section_id": "budget"})
    assert needs_hash["code"] == "hash_required"
    outline = call(registry, "doc_outline", {"path": "plan.md"})
    gone = call(registry, "doc_remove_section", {
        "path": "plan.md", "section_id": "budget", "expected_hash": section(outline, "budget")["hash"],
    })
    assert gone["removed"]["id"] == "budget"


def test_lead_is_guarded_like_a_section(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    outline = call(registry, "doc_outline", {"path": "plan.md", "section_id": LEAD_ID})
    assert outline["section"]["markdown"] == "The plan in one line."
    assert call(registry, "doc_write_section", {"path": "plan.md", "section_id": LEAD_ID, "content": "x"})["code"] == "hash_required"
    written = call(registry, "doc_write_section", {
        "path": "plan.md", "section_id": LEAD_ID, "content": "A better lead.", "expected_hash": outline["lead"]["hash"],
    })
    assert written["section"]["id"] == LEAD_ID
    assert (tmp_path / "plan.md").read_text(encoding="utf-8").startswith("# Plan\n\nA better lead.\n\n<!-- astra:section")


def test_comment_queue_round_trip(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    path = tmp_path / "plan.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("About $0.", "About $0. <!-- @astra: check the currency -->"),
        encoding="utf-8",
    )
    listed = call(registry, "doc_comments", {"path": "plan.md"})
    assert listed["open_comments"] == 1
    comment = listed["comments"][0]
    assert comment["section_id"] == "budget" and comment["context"] == "About $0."
    resolved = call(registry, "doc_resolve_comment", {"path": "plan.md", "comment_id": comment["id"], "note": "SGD"})
    assert resolved["resolved"] == {"id": comment["id"], "text": "check the currency", "note": "SGD"}
    assert resolved["open_comments"] == 0
    assert "About $0.\n" in path.read_text(encoding="utf-8")
    assert call(registry, "doc_resolve_comment", {"path": "plan.md", "comment_id": comment["id"]})["code"] == "comment_not_found"


def test_writes_are_checkpointed_and_restorable(tmp_path):
    registry = make_registry(tmp_path)
    registry.yolo = True  # checkpoint_restore always asks for approval
    create(registry)
    before = (tmp_path / "plan.md").read_text(encoding="utf-8")
    written = call(registry, "doc_write_section", {"path": "plan.md", "section_id": "method", "content": "Recorded."})
    assert written["checkpoint_id"]
    restored = json.loads(run(registry.execute("checkpoint_restore", {"checkpoint_id": written["checkpoint_id"]}))["output"])
    assert restored["status"] == "restored"
    assert (tmp_path / "plan.md").read_text(encoding="utf-8") == before


def test_paths_outside_the_workspace_need_approval(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry = make_registry(workspace)
    result = run(registry.execute("doc_outline", {"path": str(tmp_path / "elsewhere.md")}))
    assert "ToolApprovalRequired" in result["error"]


def test_read_only_roots_need_approval_before_document_writes(tmp_path):
    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    original = '# Notes\n\n<!-- astra:section id="a" status="pending" -->\n## A\n\n*Pending*\n'
    (shared / "notes.md").write_text(original, encoding="utf-8")
    policy = FilesystemPolicy(workspace, [FilesystemRoot(shared, "ro")])
    registry = make_registry(workspace, policy)
    args = {"path": str(shared / "notes.md"), "section_id": "a", "content": "Changed"}

    refused = run(registry.execute("doc_write_section", args))
    assert "ToolApprovalRequired" in refused["error"]
    assert (shared / "notes.md").read_text(encoding="utf-8") == original

    requests: list[dict] = []

    async def approve(request: dict) -> str:
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    written = call(registry, "doc_write_section", args)
    assert written["status"] == "written"
    assert requests and requests[0]["access"] == "write"
    # A once-grant does not outlive the call.
    registry.set_approval_handler(None)
    again = run(registry.execute("doc_outline", {"path": str(shared / "notes.md")}))
    assert "ToolApprovalRequired" in again["error"]


def test_doc_edit_dispatches_each_action(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    written = call(registry, "doc_edit", {"path": "plan.md", "action": "write", "section_id": "background",
                                          "content": "It matters."})
    assert written["action"] == "write" and written["section"]["id"] == "background"
    added = call(registry, "doc_edit", {"path": "plan.md", "action": "add", "heading": "Risks",
                                        "intent": "What could fail", "after": "method"})
    assert added["action"] == "add"
    assert [item["id"] for item in call(registry, "doc_outline", {"path": "plan.md"})["sections"]] == [
        "background", "method", "risks", "budget"]
    assert call(registry, "doc_edit", {"path": "plan.md", "action": "remove", "section_id": "risks"})["action"] == "remove"

    path = tmp_path / "plan.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n<!-- @astra: add numbers -->\n", encoding="utf-8")
    outline = call(registry, "doc_outline", {"path": "plan.md"})
    assert outline["open_comments"] == 1 and outline["comments"][0]["text"] == "add numbers"
    resolved = call(registry, "doc_edit", {"path": "plan.md", "action": "resolve_comment",
                                           "comment_id": outline["comments"][0]["id"], "note": "Added numbers"})
    assert resolved["action"] == "resolve_comment" and resolved["open_comments"] == 0


def test_doc_edit_requires_the_fields_of_its_action(tmp_path):
    registry = make_registry(tmp_path)
    create(registry)
    before = (tmp_path / "plan.md").read_text(encoding="utf-8")
    for args, missing in (({"action": "write", "section_id": "background"}, "content"),
                          ({"action": "add"}, "heading"),
                          ({"action": "remove"}, "section_id"),
                          ({"action": "resolve_comment"}, "comment_id")):
        result = call(registry, "doc_edit", {"path": "plan.md", **args})
        assert result.get("code") == "invalid_arguments" and missing in result["error"], (args, result)
    assert (tmp_path / "plan.md").read_text(encoding="utf-8") == before


def test_documents_group_routing(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.tool_names_for_group("documents") == ["doc_create", "doc_outline", "doc_edit", "doc_export"]
    for alias in ("doc_write_section", "doc_add_section", "doc_remove_section", "doc_comments", "doc_resolve_comment"):
        assert registry.get(alias) is not None and not registry.get(alias).expose_by_default
    for prompt in ("帮我起草一份项目提案", "write a report on sales", "把大纲导出成 PDF", "export notes.md to Word"):
        assert "documents" in registry.select_groups(prompt), prompt
    for prompt in ("documentation for the API", "fix the flaky test"):
        assert "documents" not in registry.select_groups(prompt), prompt


def create_nested(registry: ToolRegistry) -> dict:
    return call(registry, "doc_create", {"path": "nested.md", "title": "Plan", "sections": [
        {"heading": "2 Candidates", "intent": "Candidate places"},
        {"heading": "2.1 Scoring", "intent": "How we score", "level": 3},
        {"heading": "3 Budget", "intent": "Costs"},
    ]})


def statuses(registry: ToolRegistry) -> list[tuple[str, str]]:
    return [(item["id"], item["status"]) for item in call(registry, "doc_outline", {"path": "nested.md"})["sections"]]


def test_writing_a_parent_keeps_its_sub_sections(tmp_path):
    registry = make_registry(tmp_path)
    create_nested(registry)
    assert statuses(registry) == [("2-candidates", "pending"), ("2-1-scoring", "pending"), ("3-budget", "pending")]

    written = call(registry, "doc_edit", {"path": "nested.md", "action": "write", "section_id": "2-candidates",
                                          "content": "Three places."})
    assert written["action"] == "write"
    assert statuses(registry) == [("2-candidates", "written"), ("2-1-scoring", "pending"), ("3-budget", "pending")]
    text = (tmp_path / "nested.md").read_text(encoding="utf-8")
    assert text.index("Three places.") < text.index('id="2-1-scoring"') < text.index("*Pending: How we score*")

    view = call(registry, "doc_outline", {"path": "nested.md", "section_id": "2-candidates"})["section"]
    assert view["markdown"].startswith("## 2 Candidates") and "Three places." in view["markdown"]
    assert "Scoring" not in view["markdown"] and view["subsections"] == ["2-1-scoring"]


def test_a_parent_with_sub_sections_refuses_headings_and_guards_its_subtree(tmp_path):
    registry = make_registry(tmp_path)
    create_nested(registry)
    refused = call(registry, "doc_edit", {"path": "nested.md", "action": "write", "section_id": "2-candidates",
                                          "content": "Intro\n\n### Extra\n\nMore"})
    assert refused["code"] == "invalid_content" and "2-1-scoring" in refused["error"]

    call(registry, "doc_edit", {"path": "nested.md", "action": "write", "section_id": "2-1-scoring", "content": "Points."})
    outline = {item["id"]: item for item in call(registry, "doc_outline", {"path": "nested.md"})["sections"]}
    assert outline["2-candidates"]["status"] == "pending"
    # Removing the parent also removes the written child, so it needs the hash.
    assert call(registry, "doc_edit", {"path": "nested.md", "action": "remove",
                                       "section_id": "2-candidates"})["code"] == "hash_required"

    call(registry, "doc_edit", {"path": "nested.md", "action": "write", "section_id": "2-1-scoring",
                                "content": "Points, revised.", "expected_hash": outline["2-1-scoring"]["hash"]})
    stale = call(registry, "doc_edit", {"path": "nested.md", "action": "remove", "section_id": "2-candidates",
                                        "expected_hash": outline["2-candidates"]["hash"]})
    assert stale["code"] == "section_changed"
    fresh = {item["id"]: item for item in call(registry, "doc_outline", {"path": "nested.md"})["sections"]}
    removed = call(registry, "doc_edit", {"path": "nested.md", "action": "remove", "section_id": "2-candidates",
                                          "expected_hash": fresh["2-candidates"]["hash"]})
    assert removed["action"] == "remove" and statuses(registry) == [("3-budget", "pending")]
